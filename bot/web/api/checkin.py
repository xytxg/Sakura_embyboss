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
MAX_REQUESTS_PER_HOUR = 2
MAX_PAGE_LOAD_INTERVAL = 15
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
        parsed_data = {}
        for item in init_data.split('&'):
            key, value = item.split('=', 1)
            parsed_data[key] = urllib.parse.unquote(value)

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

def check_and_record_request(user_id: int, client_ip: str) -> bool:
    global redis_client

    now = int(time.time())

    try:
        if redis_client:
            user_key = f"rate_limit:user:{user_id}"
            ip_key = f"rate_limit:ip:{client_ip}"

            redis_client.zremrangebyscore(user_key, 0, now - RATE_LIMIT_WINDOW)
            redis_client.zremrangebyscore(ip_key, 0, now - RATE_LIMIT_WINDOW)

            user_count = redis_client.zcard(user_key)
            ip_count = redis_client.zcard(ip_key)
            rate_limit_window = RATE_LIMIT_WINDOW / 3600

            if user_count >= MAX_REQUESTS_PER_HOUR:
                LOGGER.info(f"❌ 用户限频 - 用户: {user_id}, {rate_limit_window} 小时记录数: {user_count}")
                return False
            if ip_count >= MAX_REQUESTS_PER_HOUR:
                LOGGER.info(f"❌ IP限频 - IP: {client_ip}, {rate_limit_window} 小时记录数: {ip_count}")
                return False

            redis_client.zadd(user_key, {str(now): now})
            redis_client.zadd(ip_key, {str(now): now})
            redis_client.expire(user_key, RATE_LIMIT_WINDOW)
            redis_client.expire(ip_key, RATE_LIMIT_WINDOW)
            return True

    except (RedisConnectionError, redis.exceptions.ResponseError) as e:
        LOGGER.warning(f"❌ Redis 频率控制失败: {e}. 回退到内存限频")
        redis_client = None

    if user_id not in user_request_records:
        user_request_records[user_id] = []
    if client_ip not in ip_request_records:
        ip_request_records[client_ip] = []

    user_request_records[user_id] = [t for t in user_request_records[user_id] if now - t < RATE_LIMIT_WINDOW]
    ip_request_records[client_ip] = [t for t in ip_request_records[client_ip] if now - t < RATE_LIMIT_WINDOW]

    user_count = len(user_request_records[user_id])
    ip_count = len(ip_request_records[client_ip])
    rate_limit_window = RATE_LIMIT_WINDOW / 3600

    if user_count >= MAX_REQUESTS_PER_HOUR:
        LOGGER.info(f"❌ 用户限频 - 用户: {user_id}, {rate_limit_window} 小时记录数: {user_count}")
        return False
    if ip_count >= MAX_REQUESTS_PER_HOUR:
        LOGGER.info(f"❌ IP限频 - IP: {client_ip}, {rate_limit_window} 小时记录数: {ip_count}")
        return False

    user_request_records[user_id].append(now)
    ip_request_records[client_ip].append(now)

    return True

def verify_request_freshness(timestamp: int, nonce: str) -> bool:
    global redis_client
    global memory_used_nonces

    current_time = time.time()
    if abs(current_time - timestamp) > MAX_REQUEST_AGE:
        return False

    nonce_key = f"nonce:{timestamp}:{nonce}"

    if redis_client:
        try:
            if not redis_client.setnx(nonce_key, 1):
                return False
            redis_client.expire(nonce_key, MAX_REQUEST_AGE)
            return True
        except (RedisConnectionError, redis.exceptions.ResponseError) as e:
            LOGGER.warning(f"❌ Redis 操作失败: {e}. 回退到内存 Nonce 检查")
            redis_client = None
            if nonce_key in memory_used_nonces:
                return False
            memory_used_nonces.add(nonce_key)
            expired = {n for n in memory_used_nonces if current_time - int(n.split(':')[1]) > MAX_REQUEST_AGE}
            memory_used_nonces -= expired
            return True
    else:
        if nonce_key in memory_used_nonces:
            return False
        memory_used_nonces.add(nonce_key)
        expired = {n for n in memory_used_nonces if current_time - int(n.split(':')[1]) > MAX_REQUEST_AGE}
        memory_used_nonces -= expired
        return True

def detect_suspicious_behavior(request: Request, user_agent: str) -> bool:
    if not user_agent or len(user_agent) < 10:
        LOGGER.info(f"❌ 可疑请求：User-Agent过短或缺失: {user_agent}")
        return True
    for pattern in ['bot', 'crawler', 'spider', 'scraper', 'wget', 'curl', 'python-requests', 'aiohttp', 'okhttp']:
        if pattern in user_agent.lower( ):
            LOGGER.info(f"❌ 可疑请求：检测到机器人User-Agent: {user_agent}")
            return True
    required_headers = ["host", "user-agent", "accept", "accept-language"]
    for header in required_headers:
        if header not in request.headers:
            LOGGER.info(f"❌ 可疑请求：缺少必要请求头: {header}")
            return True
    referer = request.headers.get("referer")
    if not referer or f"//{request.url.netloc}/api/checkin/web" not in referer:
        LOGGER.info(f"❌ 可疑请求：Referer异常或缺失: {referer}" )
        return True
    return False

def analyze_user_behavior_backend(interactions: Optional[int], session_duration: Optional[int], user_id: int, client_ip: str) -> bool:
    if interactions is None or interactions < MIN_USER_INRTEACTION:
        LOGGER.info(f"❌ 可疑行为：前端交互次数为None或过少 - 用户: {user_id}, IP: {client_ip}, 交互次数: {interactions}")
        return True
    
    session_duration_s = session_duration / 1000
    if session_duration_s is None or session_duration_s < MIN_PAGE_LOAD_INTERVAL:
        LOGGER.info(f"❌ 可疑行为：前端会话时长为None或过短 - 用户: {user_id}, IP: {client_ip}, 会话时长: {session_duration}ms")
        return True
    return False

def analyze_page_load_interval(page_load_time: Optional[int], user_id: int, client_ip: str) -> bool:
    if page_load_time is None:
        LOGGER.info(f"❌ 可疑行为：缺少页面加载时间 - 用户: {user_id}, IP: {client_ip}")
        return True

    current_time_ms = int(time.time() * 1000)
    page_load_time_ms = page_load_time

    interval_ms = current_time_ms - page_load_time_ms
    interval_s = interval_ms / 1000

    if interval_s < MIN_PAGE_LOAD_INTERVAL:
        LOGGER.info(f"❌ 可疑行为：页面加载到请求发送间隔过短 - 用户: {user_id}, IP: {client_ip}, 间隔: {interval_s:.3f}s")
        return True

    if interval_s > MAX_PAGE_LOAD_INTERVAL:
        LOGGER.info(f"❌ 可疑行为：页面加载到请求发送间隔过长 - 用户: {user_id}, IP: {client_ip}, 间隔: {interval_s:.3f}s")
        return True

    return False

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
    LOGGER.info(f"📅 签到请求 - 用户: {request_data.user_id}, IP: {client_ip}, UA: {user_agent}")

    if not _open.checkin:
        raise HTTPException(status_code=403, detail="签到功能未开启")

    if not check_and_record_request(request_data.user_id, client_ip):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    if detect_suspicious_behavior(request, user_agent):
        LOGGER.info(f"❌ 检测到可疑行为 - 用户: {request_data.user_id}, IP: {client_ip}")
        raise HTTPException(status_code=403, detail="请求被拒绝")

    if analyze_user_behavior_backend(request_data.interactions, request_data.session_duration, request_data.user_id, client_ip):
        raise HTTPException(status_code=403, detail="检测到可疑行为，请求被拒绝")

    if analyze_page_load_interval(request_data.page_load_time, request_data.user_id, client_ip):
        raise HTTPException(status_code=403, detail="检测到可疑行为，请求被拒绝")

    if not verify_request_freshness(request_data.timestamp, request_data.nonce):
        LOGGER.info(f"❌ 请求无效或已过期 - 用户: {request_data.user_id}, IP: {client_ip}, 时间戳: {request_data.timestamp}, 当前时间: {datetime.now().isoformat()}, Nonce: {request_data.nonce}")
        raise HTTPException(status_code=400, detail="请求无效或已过期")

    if request_data.webapp_data:
        try:
            webapp_info = verify_telegram_webapp_data(request_data.webapp_data)
            webapp_user_id = json.loads(webapp_info.get('user', '{}')).get('id')
            if webapp_user_id != request_data.user_id:
                raise HTTPException(status_code=401, detail="用户身份验证失败")
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.error(f"❌ WebApp数据验证错误: {e}")
            raise HTTPException(status_code=401, detail="身份验证失败")

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": TURNSTILE_SECRET_KEY,
                    "response": request_data.token,
                    "remoteip": client_ip
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                result = await response.json()
                if not result.get("success", False):
                    error_codes = result.get("error-codes", [])
                    LOGGER.info(f"❌ Turnstile验证失败 - 用户: {request_data.user_id}, 错误: {error_codes}, IP: {client_ip}")
                    raise HTTPException(status_code=400, detail="人机验证失败，请重试")
        except aiohttp.ClientError as e:
            LOGGER.error(f"❌ Turnstile验证网络错误: {e}")
            raise HTTPException(status_code=503, detail="验证服务暂时不可用")

    e = sql_get_emby(request_data.user_id)
    if not e:
        raise HTTPException(status_code=404, detail="未查询到用户数据")

    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    if e.ch and e.ch.strftime("%Y-%m-%d") >= today:
        raise HTTPException(status_code=409, detail="您今天已经签到过了，再签到剁掉你的小鸡鸡🐤")

    reward = random.randint(_open.checkin_reward[0], _open.checkin_reward[1])
    new_balance = e.iv + reward

    try:
        sql_update_emby(Emby.tg == request_data.user_id, iv=new_balance, ch=now)
    except Exception as e:
        LOGGER.error(f"数据库更新失败: {e}")
        raise HTTPException(status_code=500, detail="签到处理失败，请重试")

    LOGGER.info(f"✔️ 签到成功 - 用户: {request_data.user_id}, 奖励: {reward} {sakura_b}, IP: {client_ip}")

    checkin_text = f'🎉 **签到成功** | {reward} {sakura_b}\n💴 **当前持有** | {new_balance} {sakura_b}\n⏳ **签到日期** | {now.strftime("%Y-%m-%d")}'

    try:
        from bot import bot
        if request_data.chat_id and request_data.message_id:
            try:
                await bot.delete_messages(
                    chat_id=request_data.chat_id,
                    message_ids=request_data.message_id
                )
            except Exception as e:
                LOGGER.error(f"❌ 删除面板消息失败: {e}")
        await bot.send_message(chat_id=request_data.user_id, text=checkin_text)
    except Exception as e:
        LOGGER.error(f"❌ 发送消息失败: {e}")

    return JSONResponse({
        "success": True,
        "message": "签到成功",
        "reward": f"获得 {reward} {sakura_b}，当前持有 {new_balance} {sakura_b}",
        "should_close": True
    })