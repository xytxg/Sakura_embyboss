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
import secrets
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from bot import _open, bot_token, LOGGER, api as config_api, sakura_b 
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

# ==================== 路由与模板设置 ====================
route = APIRouter(prefix="/checkin")
templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

# ==================== 配置参数 ====================
TURNSTILE_SITE_KEY = config_api.cloudflare_turnstile.site_key or "YOUR_TURNSTILE_SITE_KEY"
TURNSTILE_SECRET_KEY = config_api.cloudflare_turnstile.secret_key or "YOUR_TURNSTILE_SECRET_KEY"
SIGNING_SECRET = secrets.token_urlsafe(32)
MAX_REQUEST_AGE = 30
RATE_LIMIT_WINDOW = 30
MAX_REQUESTS_PER_HOUR = 3

request_records: Dict[int, list] = {}
used_nonces: set = set()

# ==================== 请求模型 ====================
class CheckinVerifyRequest(BaseModel):
    token: str
    user_id: int
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
    timestamp: int
    nonce: str
    signature: str
    webapp_data: Optional[str] = None

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
        if time.time() - auth_date > 86400:
            raise HTTPException(status_code=401, detail="认证数据过期")

        return parsed_data
    except Exception as e:
        LOGGER.error(f"❌ Telegram WebApp数据验证失败: {e}")
        raise HTTPException(status_code=401, detail="数据验证失败")

def generate_request_signature(user_id: int, timestamp: int, nonce: str) -> str:
    data = f"{user_id}:{timestamp}:{nonce}"
    return hmac.new(SIGNING_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()

def verify_request_signature(user_id: int, timestamp: int, nonce: str, signature: str) -> bool:
    expected_signature = generate_request_signature(user_id, timestamp, nonce)
    return hmac.compare_digest(signature, expected_signature)

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    if user_id not in request_records:
        request_records[user_id] = []
    request_records[user_id] = [t for t in request_records[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(request_records[user_id]) >= MAX_REQUESTS_PER_HOUR:
        return False
    request_records[user_id].append(now)
    return True

def verify_request_freshness(timestamp: int, nonce: str) -> bool:
    global used_nonces
    current_time = time.time()
    if abs(current_time - timestamp) > MAX_REQUEST_AGE:
        return False
    nonce_key = f"{timestamp}:{nonce}"
    if nonce_key in used_nonces:
        return False
    used_nonces.add(nonce_key)
    expired = {n for n in used_nonces if current_time - int(n.split(':')[0]) > MAX_REQUEST_AGE}
    used_nonces -= expired
    return True

def detect_suspicious_behavior(request: Request, user_agent: str) -> bool:
    if not user_agent or len(user_agent) < 5:
        LOGGER.info(f"❌ 可疑请求：User-Agent过短或缺失: {user_agent}")
        return True
    for pattern in ['bot', 'crawler', 'spider', 'scraper', 'wget', 'curl']:
        if pattern in user_agent.lower():
            LOGGER.info(f"❌ 可疑请求：检测到机器人User-Agent: {user_agent}")
            return True
    for header in ["host", "user-agent"]:
        if header not in request.headers:
            LOGGER.info(f"❌ 可疑请求：缺少必要请求头: {header}")
            return True
    return False

# ==================== 路由处理 ====================
@route.get("/web", response_class=HTMLResponse)
async def checkin_page(request: Request):
    return templates.TemplateResponse(
        "checkin.html", 
        {"request": request, "site_key": TURNSTILE_SITE_KEY, "signing_secret": SIGNING_SECRET}
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

    if detect_suspicious_behavior(request, user_agent):
        LOGGER.info(f"❌ 检测到可疑行为 - 用户: {request_data.user_id}, IP: {client_ip}")
        raise HTTPException(status_code=403, detail="请求被拒绝")

    if not verify_request_freshness(request_data.timestamp, request_data.nonce):
        LOGGER.info(f"❌ 请求无效或已过期 - 用户: {request_data.user_id}, IP: {client_ip}, 时间戳: {request_data.timestamp}, 当前时间: {datetime.now().isoformat()}, Nonce: {request_data.nonce}")
        raise HTTPException(status_code=400, detail="请求无效或已过期")

    if not verify_request_signature(
        request_data.user_id, request_data.timestamp, request_data.nonce, request_data.signature
    ):
        LOGGER.info(f"❌ 签名验证失败 - 用户: {request_data.user_id}")
        raise HTTPException(status_code=401, detail="请求验证失败")

    if not check_rate_limit(request_data.user_id):
        LOGGER.info(f"❌ 频率限制触发 - 用户: {request_data.user_id}")
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

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

    e = sql_get_emby(request_data.user_id)
    if not e:
        raise HTTPException(status_code=404, detail="未查询到用户数据")

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