#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
checkin.py - 签到验证API路由
"""
import random
import aiohttp
import hashlib
import hmac
import time
import json
import secrets
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from pydantic import BaseModel
from typing import Optional, Dict, Any
import urllib.parse

from bot import _open, bot_token, LOGGER, api as config_api, sakura_b 
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

# 创建路由
route = APIRouter(prefix="/checkin")

# 设置模板路径
templates_path = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))

# 从配置中获取密钥
TURNSTILE_SITE_KEY = config_api.cloudflare_turnstile.site_key or "YOUR_TURNSTILE_SITE_KEY"
TURNSTILE_SECRET_KEY = config_api.cloudflare_turnstile.secret_key or "YOUR_TURNSTILE_SECRET_KEY"

# 安全配置
SIGNING_SECRET = secrets.token_urlsafe(32)
MAX_REQUEST_AGE = 30
RATE_LIMIT_WINDOW = 30
MAX_REQUESTS_PER_HOUR = 3

# 内存中的请求记录
request_records: Dict[int, list] = {}
used_nonces: set = set()

class CheckinVerifyRequest(BaseModel):
    token: str
    user_id: int
    chat_id: Optional[int] = None
    message_id: Optional[int] = None
    timestamp: int
    nonce: str
    signature: str
    webapp_data: Optional[str] = None

def verify_telegram_webapp_data(init_data: str) -> Dict[str, Any]:
    """验证Telegram WebApp initData的完整性"""
    if not init_data:
        raise HTTPException(status_code=401, detail="缺少Telegram WebApp数据")
    
    try:
        # 解析initData
        parsed_data = {}
        for item in init_data.split('&'):
            key, value = item.split('=', 1)
            parsed_data[key] = urllib.parse.unquote(value)
        
        # 提取hash
        received_hash = parsed_data.pop('hash', '')
        if not received_hash:
            raise HTTPException(status_code=401, detail="缺少数据完整性验证")
        
        # 重建数据字符串用于验证
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        
        # 计算预期的hash
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(received_hash, expected_hash):
            raise HTTPException(status_code=401, detail="Telegram数据验证失败")
        
        # 检查auth_date
        auth_date = int(parsed_data.get('auth_date', 0))
        if time.time() - auth_date > 86400:
            raise HTTPException(status_code=401, detail="认证数据过期")
        
        return parsed_data
    except Exception as e:
        LOGGER.error(f"Telegram WebApp数据验证失败: {e}")
        raise HTTPException(status_code=401, detail="数据验证失败")

def generate_request_signature(user_id: int, timestamp: int, nonce: str) -> str:
    """生成请求签名"""
    data = f"{user_id}:{timestamp}:{nonce}"
    return hmac.new(SIGNING_SECRET.encode(), data.encode(), hashlib.sha256).hexdigest()

def verify_request_signature(user_id: int, timestamp: int, nonce: str, signature: str) -> bool:
    """验证请求签名"""
    expected_signature = generate_request_signature(user_id, timestamp, nonce)
    return hmac.compare_digest(signature, expected_signature)

def check_rate_limit(user_id: int) -> bool:
    """检查用户请求频率限制"""
    now = time.time()
    
    if user_id not in request_records:
        request_records[user_id] = []
    
    # 清理过期记录
    request_records[user_id] = [
        req_time for req_time in request_records[user_id] 
        if now - req_time < RATE_LIMIT_WINDOW
    ]
    
    # 检查是否超过限制
    if len(request_records[user_id]) >= MAX_REQUESTS_PER_HOUR:
        return False
    
    # 记录当前请求
    request_records[user_id].append(now)
    return True

def verify_request_freshness(timestamp: int, nonce: str) -> bool:
    """验证请求时效性和唯一性"""
    global used_nonces
    current_time = time.time()
    
    # 检查时间戳是否在有效范围内
    if abs(current_time - timestamp) > MAX_REQUEST_AGE:
        return False
    
    # 检查nonce是否已使用（防重放攻击）
    nonce_key = f"{timestamp}:{nonce}"
    if nonce_key in used_nonces:
        return False
    
    used_nonces.add(nonce_key)
    
    # 清理过期的nonce（避免内存泄漏）
    expired_nonces = {
        n for n in used_nonces 
        if current_time - int(n.split(':')[0]) > MAX_REQUEST_AGE
    }
    used_nonces -= expired_nonces
    
    return True

def detect_suspicious_behavior(request: Request, user_agent: str) -> bool:
    """检测可疑行为 - 放宽检测条件"""
    # 基本的User-Agent检查
    if not user_agent or len(user_agent) < 5:
        LOGGER.info(f"可疑请求：User-Agent过短或缺失: {user_agent}")
        return True
    
    # 检查是否为明显的机器人UA
    suspicious_ua_patterns = [
        'bot', 'crawler', 'spider', 'scraper', 'wget', 'curl'
    ]
    
    ua_lower = user_agent.lower()
    for pattern in suspicious_ua_patterns:
        if pattern in ua_lower:
            LOGGER.info(f"可疑请求：检测到机器人User-Agent: {user_agent}")
            return True
    
    # 检查基本的请求头
    required_headers = ["host", "user-agent"]
    for header in required_headers:
        if header not in request.headers:
            LOGGER.info(f"可疑请求：缺少必要请求头: {header}")
            return True
    
    return False

@route.get("/web", response_class=HTMLResponse)
async def checkin_page(request: Request):
    """签到页面"""
    return templates.TemplateResponse(
        "checkin.html", 
        {
            "request": request, 
            "site_key": TURNSTILE_SITE_KEY,
            "signing_secret": SIGNING_SECRET
        }
    )

@route.post("/verify")
async def verify_checkin(
    request_data: CheckinVerifyRequest, 
    request: Request,
    user_agent: str = Header(None),
    x_forwarded_for: str = Header(None)
):
    """验证签到"""
    client_ip = x_forwarded_for.split(',')[0].strip() if x_forwarded_for else request.client.host
    LOGGER.info(f"签到请求 - 用户: {request_data.user_id}, IP: {client_ip}, UA: {user_agent}")
    
    # 检查签到功能是否开启
    if not _open.checkin:
        raise HTTPException(status_code=403, detail="签到功能未开启")
    
    # 检测可疑行为
    if detect_suspicious_behavior(request, user_agent):
        LOGGER.info(f"检测到可疑行为 - 用户: {request_data.user_id}, IP: {client_ip}")
        raise HTTPException(status_code=403, detail="请求被拒绝")
    
    # 验证请求时效性和唯一性
    if not verify_request_freshness(request_data.timestamp, request_data.nonce):
        raise HTTPException(status_code=400, detail="请求无效或已过期")
    
    # 验证请求签名
    if not verify_request_signature(
        request_data.user_id, 
        request_data.timestamp, 
        request_data.nonce, 
        request_data.signature
    ):
        LOGGER.info(f"签名验证失败 - 用户: {request_data.user_id}")
        raise HTTPException(status_code=401, detail="请求验证失败")
    
    # 检查频率限制
    if not check_rate_limit(request_data.user_id):
        LOGGER.info(f"频率限制触发 - 用户: {request_data.user_id}")
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    
    # 验证Telegram WebApp数据
    if request_data.webapp_data:
        try:
            webapp_info = verify_telegram_webapp_data(request_data.webapp_data)
            webapp_user_id = json.loads(webapp_info.get('user', '{}')).get('id')
            if webapp_user_id != request_data.user_id:
                raise HTTPException(status_code=401, detail="用户身份验证失败")
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.error(f"WebApp数据验证错误: {e}")
            raise HTTPException(status_code=401, detail="身份验证失败")
    
    # 检查用户是否存在
    e = sql_get_emby(request_data.user_id)
    if not e:
        raise HTTPException(status_code=404, detail="未查询到用户数据")
    
    # 验证 Cloudflare Turnstile
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
                    LOGGER.info(f"Turnstile验证失败 - 用户: {request_data.user_id}, 错误: {error_codes}, IP: {client_ip}")
                    raise HTTPException(status_code=400, detail="人机验证失败，请重试")
        except aiohttp.ClientError as e:
            LOGGER.error(f"Turnstile验证网络错误: {e}")
            raise HTTPException(status_code=503, detail="验证服务暂时不可用")
    
    # 处理签到逻辑
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    
    # 检查今天是否已经签到
    if e.ch and e.ch.strftime("%Y-%m-%d") >= today:
        raise HTTPException(status_code=409, detail="您今天已经签到过了，再签到剁掉你的小鸡鸡🐤")
    
    # 处理签到奖励
    reward = random.randint(_open.checkin_reward[0], _open.checkin_reward[1])
    new_balance = e.iv + reward
    
    # 更新emby表
    try:
        sql_update_emby(Emby.tg == request_data.user_id, iv=new_balance, ch=now)
    except Exception as e:
        LOGGER.error(f"数据库更新失败: {e}")
        raise HTTPException(status_code=500, detail="签到处理失败，请重试")
    
    LOGGER.info(f"签到成功 - 用户: {request_data.user_id}, 奖励: {reward}, IP: {client_ip}")
    
    # 构建签到成功消息
    checkin_text = f'🎉 **签到成功** | {reward} {sakura_b}\n💴 **当前持有** | {new_balance} {sakura_b}\n⏳ **签到日期** | {now.strftime("%Y-%m-%d")}'
    
    # 发送通知消息
    try:
        from bot import bot
        
        # 删除面板消息
        if request_data.chat_id and request_data.message_id:
            try:
                await bot.delete_messages(
                    chat_id=request_data.chat_id,
                    message_ids=request_data.message_id
                )
            except Exception as e:
                LOGGER.error(f"删除面板消息失败: {e}")
        
        # 发送签到成功消息
        await bot.send_message(
            chat_id=request_data.user_id,
            text=checkin_text
        )
    except Exception as e:
        LOGGER.error(f"发送消息失败: {e}")
    
    return JSONResponse({
        "success": True,
        "message": "签到成功",
        "reward": f"获得 {reward} {sakura_b}，当前持有 {new_balance} {sakura_b}",
        "should_close": True
    })