#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
checkin.py - 签到验证 API 路由
"""

# ==================== 导入模块 ====================
import random
import aiohttp
import hashlib
import hmac
import time
import json
import redis
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

from bot import _open, bot_token, LOGGER, api as config_api, sakura_b
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

# ==================== 路由与模板设置 ====================
route = APIRouter(prefix="/checkin" )
templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

# ==================== 配置参数 ====================
TURNSTILE_SITE_KEY = config_api.cloudflare_turnstile.site_key
TURNSTILE_SECRET_KEY = config_api.cloudflare_turnstile.secret_key

SIGNING_SECRET = config_api.singing_secret

MAX_REQUEST_AGE = 5
RATE_LIMIT_WINDOW = 3600
MAX_REQUESTS_PER_HOUR = 3
MAX_PAGE_LOAD_INTERVAL = 17
MIN_PAGE_LOAD_INTERVAL = 3
MIN_USER_INRTEACTION = 3

REDIS_HOST = config_api.redis.host
REDIS_PORT = config_api.redis.port
REDIS_DB = config_api.redis.db
REDIS_PASSWORD = config_api.redis.password
DECODE_RESPONSES = config_api.redis.decode_responses

redis_client = None
try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        password=REDIS_PASSWORD,
        decode_responses=DECODE_RESPONSES
    )
    redis_client.ping()
    LOGGER.info("✅ Redis 连接成功！")
except (RedisConnectionError, redis.exceptions.ResponseError) as e:
    LOGGER.warning(f"❌ Redis 连接或认证失败: {e}. 将使用内存存储 Nonce")
    redis_client = None

user_request_records: Dict[int, list] = {}
ip_request_records: Dict[str, list] = {}
memory_used_nonces: set = set()

# ==================== 请求模型 ====================
class CheckinVerifyRequest(BaseModel):
    token: str
    user_id: int
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
    timestamp: int
    nonce: str
    webapp_data: Optional[str] = None
    interactions: Optional[int] = None
    session_duration: Optional[int] = None
    page_load_time: Optional[int] = None

# ==================== 工具函数 ====================
def verify_telegram_webapp_data(init_data: str) -> Dict[str, Any]:
    if not init_data:
        raise HTTPException(status_code=401, detail="缺少Telegram WebApp数据")

    try:
        parsed_data = {k: urllib.parse.unquote(v) for k, v in (item.split('=', 1) for item in init_data.split('&'))}
        received_hash = parsed_data.pop('hash', '')
        if not received_hash:
            raise HTTPException(status_code=401, detail="缺少数据完整性验证")

        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, expected_hash):
            raise HTTPException(status_code=401, detail="Telegram数据验证失败")

        auth_date = int(parsed_data.get('auth_date', 0))
        if time.time() - auth_date > 3600:
            raise HTTPException(status_code=401, detail="认证数据过期")

        return parsed_data
    except Exception as e:
        LOGGER.error(f"❌ Telegram WebApp数据验证失败: {e}")
        raise HTTPException(status_code=401, detail="数据验证失败")

def check_and_record_request(user_id: int, client_ip: str) -> Optional[str]:
    global redis_client
    now = int(time.time())
    
    try:
        if redis_client:
            user_key = f"rate_limit:user:{user_id}"
            ip_key = f"rate_limit:ip:{client_ip}"
            
            pipe = redis_client.pipeline()
            pipe.zremrangebyscore(user_key, 0, now - RATE_LIMIT_WINDOW)
            pipe.zremrangebyscore(ip_key, 0, now - RATE_LIMIT_WINDOW)
            pipe.zcard(user_key)
            pipe.zcard(ip_key)
            results = pipe.execute()
            
            user_count, ip_count = results[2], results[3]
            
            if user_count >= MAX_REQUESTS_PER_HOUR:
                return "user_rate_limited"
            if ip_count >= MAX_REQUESTS_PER_HOUR:
                return "ip_rate_limited"
            
            pipe = redis_client.pipeline()
            pipe.zadd(user_key, {str(now): now})
            pipe.zadd(ip_key, {str(now): now})
            pipe.expire(user_key, RATE_LIMIT_WINDOW)
            pipe.expire(ip_key, RATE_LIMIT_WINDOW)
            pipe.execute()
            return None
    except (RedisConnectionError, redis.exceptions.ResponseError) as e:
        LOGGER.warning(f"🟡 Redis 频率控制失败: {e}. 回退到内存限频。")
        redis_client = None

    user_request_records.setdefault(user_id, [])
    ip_request_records.setdefault(client_ip, [])
    user_request_records[user_id] = [t for t in user_request_records[user_id] if now - t < RATE_LIMIT_WINDOW]
    ip_request_records[client_ip] = [t for t in ip_request_records[client_ip] if now - t < RATE_LIMIT_WINDOW]
    
    if len(user_request_records[user_id]) >= MAX_REQUESTS_PER_HOUR:
        return "user_rate_limited"
    if len(ip_request_records[client_ip]) >= MAX_REQUESTS_PER_HOUR:
        return "ip_rate_limited"

    user_request_records[user_id].append(now)
    ip_request_records[client_ip].append(now)
    return None

def verify_request_freshness(timestamp: int, nonce: str) -> bool:
    global redis_client
    global memory_used_nonces

    current_time = time.time()
    if abs(current_time - timestamp) > MAX_REQUEST_AGE:
        return False

    if redis_client:
        try:
            redis_nonce_key = f"nonce:{nonce}"
            if not redis_client.set(redis_nonce_key, 1, ex=MAX_REQUEST_AGE, nx=True):
                return False
            return True
        except (RedisConnectionError, redis.exceptions.ResponseError) as e:
            LOGGER.warning(f"🟡 Redis Nonce 操作失败: {e}. 回退到内存检查。")
            redis_client = None

    mem_nonce_key = f"nonce:{timestamp}:{nonce}"

    if mem_nonce_key in memory_used_nonces:
        return False
    
    memory_used_nonces.add(mem_nonce_key)

    if random.random() < 0.01:
        expired_nonces = {
            n for n in memory_used_nonces 
            if current_time - int(n.split(':')[1]) > MAX_REQUEST_AGE
        }
        if expired_nonces:
            memory_used_nonces.difference_update(expired_nonces)
            LOGGER.debug(f"内存Nonce清理完成，移除了 {len(expired_nonces)} 个过期Nonce。")

    return True

def run_all_security_checks(request: Request, data: CheckinVerifyRequest, user_agent: str) -> Optional[str]:
    if not user_agent or len(user_agent) < 10: return f"UA过短或缺失"
    for pattern in ['bot', 'crawler', 'spider', 'scraper', 'wget', 'curl', 'python-requests', 'aiohttp', 'okhttp']:
        if pattern in user_agent.lower( ): return f"检测到 {pattern} UA"
    for header in ["host", "user-agent", "accept", "accept-language"]:
        if header not in request.headers: return f"缺少 {header} 请求头"
    
    if data.interactions is None or data.interactions < MIN_USER_INRTEACTION: return f"前端交互仅 {data.interactions} 次"
    if data.session_duration is None or (data.session_duration / 1000) < MIN_PAGE_LOAD_INTERVAL: return f"前端会话时长仅 {data.session_duration}ms"

    if data.page_load_time is None: return "缺少页面加载时间"
    interval_s = (int(time.time() * 1000) - data.page_load_time) / 1000
    if not (MIN_PAGE_LOAD_INTERVAL <= interval_s <= MAX_PAGE_LOAD_INTERVAL): return f"请求间隔为 {interval_s:.3f}s"

    if not verify_request_freshness(data.timestamp, data.nonce): return f"请求无效或已过期 (Nonce)"

    return None

# ==================== 路由处理 ====================
@route.get("/web", response_class=HTMLResponse)
async def checkin_page(request: Request):
    return templates.TemplateResponse(
        "checkin.html",
        {"request": request, "site_key": TURNSTILE_SITE_KEY}
    )

@route.post("/verify")
async def verify_checkin(
    request_data: CheckinVerifyRequest,
    request: Request,
    user_agent: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    client_ip = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else request.client.host
    log_base_info = f"用户: {request_data.user_id}, IP: {client_ip}, UA: {user_agent}"

    try:
        if not _open.checkin:
            LOGGER.warning(f"⚠️ 签到失败 (功能未开启) - {log_base_info}")
            raise HTTPException(status_code=403, detail="签到功能未开启")

        rate_limit_reason = check_and_record_request(request_data.user_id, client_ip)
        if rate_limit_reason:
            detail_message = "请求过于频繁，请稍后再试"
            if rate_limit_reason == "user_rate_limited":
                detail_message = "您的签到请求过于频繁，请稍后再试"
            elif rate_limit_reason == "ip_rate_limited":
                detail_message = "当前IP地址请求过于频繁，请稍后再试"
            LOGGER.warning(f"⚠️ 签到失败 (请求频繁: {rate_limit_reason}) - {log_base_info})")
            raise HTTPException(status_code=429, detail=detail_message)

        suspicion_reason = run_all_security_checks(request, request_data, user_agent)
        if suspicion_reason:
            LOGGER.warning(f"⚠️ 签到失败 (可疑行为: {suspicion_reason}) - {log_base_info}")
            raise HTTPException(status_code=403, detail="检测到可疑行为，请求被拒绝")

        if request_data.webapp_data:
            try:
                webapp_info = verify_telegram_webapp_data(request_data.webapp_data)
                webapp_user_id = json.loads(webapp_info.get('user', '{}')).get('id')
                if webapp_user_id != request_data.user_id:
                    LOGGER.warning(f"⚠️ 签到失败 (身份验证失败) - {log_base_info}")
                    raise HTTPException(status_code=401, detail="用户身份验证失败")
            except HTTPException as e:
                if e.status_code != 401: LOGGER.error(f"❌ WebApp数据验证错误: {e.detail}")
                raise

        async with aiohttp.ClientSession( ) as session:
            try:
                async with session.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data={"secret": TURNSTILE_SECRET_KEY, "response": request_data.token, "remoteip": client_ip},
                    timeout=aiohttp.ClientTimeout(total=10 )
                ) as response:
                    result = await response.json()
                    if not result.get("success", False):
                        error_codes = result.get("error-codes", [])
                        LOGGER.warning(f"⚠️ 签到失败 (人机验证: {error_codes}) - {log_base_info}")
                        raise HTTPException(status_code=400, detail="人机验证失败，请重试")
            except aiohttp.ClientError as e:
                LOGGER.error(f"❌ Turnstile验证网络错误: {e}" )
                raise HTTPException(status_code=503, detail="验证服务暂时不可用")

        e = sql_get_emby(request_data.user_id)
        if not e:
            LOGGER.warning(f"⚠️ 签到失败 (用户不存在) - {log_base_info}")
            raise HTTPException(status_code=404, detail="未查询到用户数据")

        now = datetime.now(timezone(timedelta(hours=8)))
        today = now.strftime("%Y-%m-%d")
        if e.ch and e.ch.strftime("%Y-%m-%d") >= today:
            LOGGER.info(f"ℹ️ 签到中止 (今日已签) - {log_base_info}")
            raise HTTPException(status_code=409, detail="您今天已经签到过了，再签到剁掉你的小鸡鸡🐤")

        reward = random.randint(_open.checkin_reward[0], _open.checkin_reward[1])
        new_balance = e.iv + reward

        try:
            sql_update_emby(Emby.tg == request_data.user_id, iv=new_balance, ch=now)
        except Exception as db_err:
            LOGGER.error(f"❌ 签到失败 (数据库更新错误: {db_err}) - {log_base_info}")
            raise HTTPException(status_code=500, detail="签到处理失败，请重试")

        LOGGER.info(f"✔️ 签到成功 (奖励: {reward} {sakura_b}) - {log_base_info}")

        checkin_text = f'🎉 **签到成功** | {reward} {sakura_b}\n💴 **当前持有** | {new_balance} {sakura_b}\n⏳ **签到日期** | {now.strftime("%Y-%m-%d")}'

        try:
            from bot import bot
            if request_data.chat_id and request_data.message_id:
                await bot.delete_messages(chat_id=request_data.chat_id, message_ids=request_data.message_id)
            await bot.send_message(chat_id=request_data.user_id, text=checkin_text)
        except Exception as tg_err:
            LOGGER.error(f"❌ 发送TG消息失败: {tg_err}")

        return JSONResponse({
            "success": True,
            "message": "签到成功",
            "reward": f"获得 {reward} {sakura_b}，当前持有 {new_balance} {sakura_b}",
            "should_close": True
        })

    except HTTPException:
        raise
    except Exception as final_err:
        LOGGER.error(f"💥 签到失败 (未知错误: {final_err}) - {log_base_info}")
        raise HTTPException(status_code=500, detail="服务器内部错误")