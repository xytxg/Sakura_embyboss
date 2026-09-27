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
from bot import bot
from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

from bot import _open, bot_token, LOGGER, api as config_api, sakura_b
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

# ==================== 路由与模板设置 ====================
route = APIRouter()
templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

# ==================== 配置参数 ====================
TURNSTILE_SITE_KEY = config_api.cloudflare_turnstile.site_key
TURNSTILE_SECRET_KEY = config_api.cloudflare_turnstile.secret_key

RECAPTCHA_V3_SITE_KEY = config_api.google_recaptcha_v3.site_key
RECAPTCHA_V3_SECRET_KEY = config_api.google_recaptcha_v3.secret_key

SIGNING_SECRET = config_api.singing_secret

MAX_REQUEST_AGE = 5
RATE_LIMIT_WINDOW = 900
MAX_REQUESTS_PER_HOUR = 3
MAX_PAGE_LOAD_INTERVAL = 30
MIN_PAGE_LOAD_INTERVAL = 1
MIN_USER_INRTEACTION = 3

REDIS_HOST = config_api.redis.host
REDIS_PORT = config_api.redis.port
REDIS_DB = config_api.redis.db
REDIS_PASSWORD = config_api.redis.password
DECODE_RESPONSES = config_api.redis.decode_responses

TG_LOG_BOT_TOKEN = config_api.log_to_tg.bot_token
TG_LOG_CHAT_ID = config_api.log_to_tg.chat_id
TG_LOG_CHECKIN_THREAD_ID = config_api.log_to_tg.checkin_thread_id
_TG_LOG_CONFIG_MISSING_WARNING_SHOWN = False

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

from cacheout import Cache

user_request_records: Dict[int, list] = {}
ip_request_records: Dict[str, list] = {}
memory_used_nonces = Cache(maxsize=10000, ttl=MAX_REQUEST_AGE * 2)

# ==================== 请求模型 ====================
class CheckinVerifyRequest(BaseModel):
    turnstile_token: str
    recaptcha_v3_token: Optional[str] = None
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
async def send_log_to_tg(log_type: str, user_id: int, reason: str = "", ip: str = "N/A", ua: str = "N/A"):
    global _TG_LOG_CONFIG_MISSING_WARNING_SHOWN

    if not TG_LOG_BOT_TOKEN or not TG_LOG_CHAT_ID:
        if not _TG_LOG_CONFIG_MISSING_WARNING_SHOWN:
            LOGGER.warning("TG Token 或 Chat ID 未配置，将跳过发送日志")
            _TG_LOG_CONFIG_MISSING_WARNING_SHOWN = True
        return

    user_name = "无法获取昵称"
    tg_username = "无"
    try:
        chat_info = await bot.get_chat(user_id)
        user_name = chat_info.first_name
        if chat_info.username:
            tg_username = chat_info.username
    except Exception as e:
        LOGGER.error(f"通过 user_id {user_id} 获取TG信息失败: {e}")

    now_str = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    text = (
        f"#用户签到通知\n\n"
        f"📅 *签到结果:* {log_type}\n"
        f"🕒 *签到时间:* `{now_str}`\n"
        f"🌍 *用户 IP:* `{ip}`\n"
        f"👤 *TG 信息:*\n"
        f"   - *昵称:* `{user_name}` (`{user_id}`)\n"
        f"   - *用户名:* `{tg_username}`\n"
        f"   - *深链接:* tg://user?id={user_id}\n"
        f"```UserAgent\n{ua}```"
    )
    if reason:
        text += f"\n📝 {reason}"

    url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TG_LOG_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    if TG_LOG_CHECKIN_THREAD_ID:
        payload['message_thread_id'] = TG_LOG_CHECKIN_THREAD_ID

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as response:
                if response.status == 200:
                    return

                response_data = await response.json()
                error_desc = response_data.get('description', '未知API错误')
                LOGGER.error(
                    f"❌ 发送TG日志失败！"
                    f"状态码: {response.status}, 原因: {error_desc}"
                )

    except aiohttp.ClientError as e:
        LOGGER.error(f"❌ 发送TG日志时发生网络错误: {e}，请检查网络连接或域名解析")
    except Exception as e:
        LOGGER.error(f"❌ 发送TG日志时发生未知错误: {e}")

def verify_telegram_webapp_data(init_data: str) -> Dict[str, Any]:
    if not init_data:
        raise HTTPException(status_code=401, detail="请求异常，请重试")

    try:
        parsed_data = {k: urllib.parse.unquote(v) for k, v in (item.split('=', 1) for item in init_data.split('&'))}
        received_hash = parsed_data.pop('hash', '')
        if not received_hash:
            raise HTTPException(status_code=401, detail="请求异常，请重试")

        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, expected_hash):
            raise HTTPException(status_code=401, detail="请求异常，请重试")

        auth_date = int(parsed_data.get('auth_date', 0))
        if time.time() - auth_date > 3600:
            raise HTTPException(status_code=401, detail="请求异常，请重试")

        return parsed_data
    except Exception as e:
        LOGGER.error(f"❌ Telegram WebApp数据验证失败: {e}")
        raise HTTPException(status_code=401, detail="请求异常，请重试")

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
                return "用户请求频繁"
            if ip_count >= MAX_REQUESTS_PER_HOUR:
                return "IP请求频繁"
            
            pipe = redis_client.pipeline()
            pipe.zadd(user_key, {str(now): now})
            pipe.zadd(ip_key, {str(now): now})
            pipe.expire(user_key, RATE_LIMIT_WINDOW)
            pipe.expire(ip_key, RATE_LIMIT_WINDOW)
            pipe.execute()
            return None
    except (RedisConnectionError, redis.exceptions.ResponseError) as e:
        LOGGER.warning(f"🟡 Redis 频率控制失败: {e}. 回退到内存限频")
        redis_client = None

    user_request_records.setdefault(user_id, [])
    ip_request_records.setdefault(client_ip, [])
    user_request_records[user_id] = [t for t in user_request_records[user_id] if now - t < RATE_LIMIT_WINDOW]
    ip_request_records[client_ip] = [t for t in ip_request_records[client_ip] if now - t < RATE_LIMIT_WINDOW]
    
    if len(user_request_records[user_id]) >= MAX_REQUESTS_PER_HOUR:
        return "用户请求频繁"
    if len(ip_request_records[client_ip]) >= MAX_REQUESTS_PER_HOUR:
        return "IP请求频繁"

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
            LOGGER.warning(f"🟡 Redis Nonce 操作失败: {e}. 回退到内存检查")
            redis_client = None

    if memory_used_nonces.get(nonce):
        return False
    memory_used_nonces.set(nonce, True)
    return True

async def verify_recaptcha_v3(token: str, client_ip: str) -> (bool, float, Optional[str]):
    if not RECAPTCHA_V3_SECRET_KEY or not token:
        return False, -1.0, "服务器未配置reCAPTCHAv3或客户端未提供token"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data={
                    "secret": RECAPTCHA_V3_SECRET_KEY,
                    "response": token,
                    "remoteip": client_ip
                },
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                result = await response.json()
                
                success = result.get("success", False)
                score = result.get("score", 0.0)
                
                if success and score >= 0.3:
                    return True, score, None
                else:
                    reason = f"reCAPTCHAv3验证失败: success={success}, score={score}"
                    return False, score, reason
                    
    except aiohttp.ClientError as e:
        reason = f"reCAPTCHA v3验证网络错误: {e}"
        LOGGER.error(reason)
        return False, -1.0, reason
    except Exception as e:
        reason = f"reCAPTCHA v3验证未知错误: {e}"
        LOGGER.error(reason)
        return False, -1.0, reason

def run_all_security_checks(request: Request, data: CheckinVerifyRequest, user_agent: str) -> Optional[str]:
    if not user_agent or len(user_agent) < 10: return f"UA过短或缺失"
    for pattern in ['bot', 'crawler', 'spider', 'scraper', 'wget', 'curl', 'python-requests', 'aiohttp', 'okhttp']:
        if pattern in user_agent.lower(): return f"检测到 {pattern} UA"
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
        {
            "request": request, 
            "turnstile_site_key": TURNSTILE_SITE_KEY,
            "recaptcha_v3_site_key": RECAPTCHA_V3_SITE_KEY
        }
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
    recaptcha_v3_score = -1.0

    try:
        if not _open.checkin:
            reason = "签到功能未开启"
            LOGGER.warning(f"⚠️ 签到失败 ({reason}) - {log_base_info}")
            await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
            raise HTTPException(status_code=403, detail=reason)

        rate_limit_reason = check_and_record_request(request_data.user_id, client_ip)
        if rate_limit_reason:
            detail_message = "请求过于频繁，请稍后重试"
            if rate_limit_reason == "用户请求频繁":
                detail_message = "您的签到请求过于频繁，请稍后重试"
            elif rate_limit_reason == "IP请求频繁":
                detail_message = "当前IP地址请求过于频繁，请稍后重试"
            LOGGER.warning(f"⚠️ 签到失败 (请求频繁: {rate_limit_reason}) - {log_base_info})")
            await send_log_to_tg('❌ 失败', request_data.user_id, f"请求频繁: {rate_limit_reason}", client_ip, user_agent)
            raise HTTPException(status_code=429, detail=detail_message)

        suspicion_reason = run_all_security_checks(request, request_data, user_agent)
        if suspicion_reason:
            LOGGER.warning(f"⚠️ 签到失败 (可疑行为: {suspicion_reason}) - {log_base_info}")
            await send_log_to_tg('❌ 失败', request_data.user_id, f"可疑行为: {suspicion_reason}", client_ip, user_agent)
            raise HTTPException(status_code=403, detail="请求异常，请重试")

        if request_data.webapp_data:
            try:
                webapp_info = verify_telegram_webapp_data(request_data.webapp_data)
                webapp_user_id = json.loads(webapp_info.get('user', '{}')).get('id')
                if webapp_user_id != request_data.user_id:
                    reason = "WebApp用户身份与请求不匹配"
                    LOGGER.warning(f"⚠️ 签到失败 ({reason}) - {log_base_info}")
                    await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
                    raise HTTPException(status_code=401, detail="请求异常，请重试")
            except HTTPException as e:
                if e.status_code != 401: LOGGER.error(f"❌ WebApp数据验证错误: {e.detail}")
                await send_log_to_tg('❌ 失败', request_data.user_id, f"WebApp验证失败: {e.detail}", client_ip, user_agent)
                raise

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    data={"secret": TURNSTILE_SECRET_KEY, "response": request_data.turnstile_token, "remoteip": client_ip},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    result = await response.json()
                    if not result.get("success", False):
                        error_codes = result.get("error-codes", [])
                        reason = f"Turnstile人机验证失败: {error_codes}"
                        LOGGER.warning(f"⚠️ 签到失败 ({reason}) - {log_base_info}")
                        await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
                        raise HTTPException(status_code=400, detail="请求异常，请重试")
            except aiohttp.ClientError as e:
                reason = f"Turnstile验证网络错误: {e}"
                LOGGER.error(f"❌ {reason}")
                await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
                raise HTTPException(status_code=503, detail="服务异常，请重试")

        if RECAPTCHA_V3_SITE_KEY and RECAPTCHA_V3_SECRET_KEY:
            if not request_data.recaptcha_v3_token:
                reason = "缺少reCAPTCHAv3验证"
                LOGGER.warning(f"⚠️ 签到失败 ({reason}) - {log_base_info}")
                await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
                raise HTTPException(status_code=400, detail="请求异常，请重试")
            
            recaptcha_v3_valid, recaptcha_v3_score, recaptcha_v3_reason = await verify_recaptcha_v3(request_data.recaptcha_v3_token, client_ip)
            if not recaptcha_v3_valid:
                reason = recaptcha_v3_reason or "reCAPTCHAv3验证失败"
                LOGGER.warning(f"⚠️ 签到失败 ({reason}) - {log_base_info}")
                await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
                raise HTTPException(status_code=400, detail="请求异常，请重试")

        e = sql_get_emby(request_data.user_id)
        if not e:
            reason = "用户不存在于数据库"
            LOGGER.warning(f"⚠️ 签到失败 ({reason}) - {log_base_info}")
            await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
            raise HTTPException(status_code=404, detail="请求异常，请重试")

        now = datetime.now(timezone(timedelta(hours=8)))
        today = now.strftime("%Y-%m-%d")
        if e.ch and e.ch.strftime("%Y-%m-%d") >= today:
            reason = "今日已签到"
            log_reason = f"{reason}: reCAPTCHAv3 - {recaptcha_v3_score}分" if recaptcha_v3_score != -1.0 else reason
            LOGGER.info(f"ℹ️ 签到中止 ({log_reason}) - {log_base_info}")
            await send_log_to_tg('ℹ️ 已签', request_data.user_id, log_reason, client_ip, user_agent)
            raise HTTPException(status_code=409, detail="您今天已经签到过了，再签到剁掉你的小鸡鸡🐤")

        reward = random.randint(_open.checkin_reward[0], _open.checkin_reward[1])
        new_balance = e.iv + reward

        try:
            sql_update_emby(Emby.tg == request_data.user_id, iv=new_balance, ch=now)
        except Exception as db_err:
            reason = f"数据库更新错误: {db_err}"
            LOGGER.error(f"❌ 签到失败 ({reason}) - {log_base_info}")
            await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
            raise HTTPException(status_code=500, detail="服务异常，请重试")

        verification_methods = ["Turnstile"]
        if RECAPTCHA_V3_SITE_KEY and RECAPTCHA_V3_SECRET_KEY:
            verification_methods.append(f"reCAPTCHAv3 - {recaptcha_v3_score:.1f}分")
        verification_info = " + ".join(verification_methods)
        
        success_reason = f"奖励: {reward} {sakura_b}, 余额: {new_balance} {sakura_b}, 验证: {verification_info}"
        LOGGER.info(f"✔️ 签到成功 ({success_reason}) - {log_base_info}")
        await send_log_to_tg('✅ 成功', request_data.user_id, success_reason, client_ip, user_agent)

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

    except HTTPException as http_ex:
        raise http_ex
    except Exception as e:
        reason = f"未知错误: {e}"
        LOGGER.error(f"❌ 签到失败 ({reason}) - {log_base_info}")
        await send_log_to_tg('❌ 失败', request_data.user_id, reason, client_ip, user_agent)
        raise HTTPException(status_code=500, detail="服务器内部错误")