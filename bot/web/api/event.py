#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
event.py - Emby Webhook 事件处理
"""
import re
import time
import pytz
import aiohttp
import asyncio
from typing import Optional, Tuple
from datetime import datetime, timedelta
from pyrogram.enums import ParseMode
from pyrogram.errors import PeerIdInvalid
from bot import LOGGER, bot, owner, api as config_api
from bot.sql_helper.sql_emby import sql_get_emby
from bot.sql_helper.sql_emby2 import sql_get_emby2
from fastapi import APIRouter, Request, Response, HTTPException
from bot.func_helper.shared_cache import host_cache, play_session_cache, ip_cache, PLAY_SESSION_MAX_SIZE

route = APIRouter()

# --- 配置加载 ---
TG_LOG_BOT_TOKEN = config_api.log_to_tg.bot_token
TG_LOG_CHAT_ID = config_api.log_to_tg.chat_id
TG_LOGIN_THREAD_ID = config_api.log_to_tg.login_thread_id
TG_PLAY_THREAD_ID = config_api.log_to_tg.play_thread_id
IGNORED_USERS_SET = config_api.log_to_tg.ignore_users
EXPIRED_LOGIN_ALERT_COOLDOWN = 3600
expired_login_alerts = {}

# --- 事件常量 ---
EVENT_USER_AUTHENTICATED = 'user.authenticated'
EVENT_PLAYBACK_START = 'playback.start'
EVENT_PLAYBACK_STOP = 'playback.stop'
EVENT_PLAYBACK_PAUSE = 'playback.pause'
EVENT_SESSION_ENDED = 'playback.sessionended'

# --- 工具函数 ---
def parse_utc_to_beijing(utc_str: str) -> Optional[datetime]:
    try:
        match = re.search(r"(\.\d{6})\d*Z?$", utc_str)
        if match:
            utc_str = utc_str[:match.start(1)] + match.group(1) + "Z"

        utc_time = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return utc_time.astimezone(pytz.timezone("Asia/Shanghai")).replace(tzinfo=None)
    except Exception as e:
        LOGGER.error(f"时间转换失败: {e}, 原始字符串: '{utc_str}'")
        return None

def get_expiry_check_time(expiry: datetime) -> datetime:
    if expiry.tzinfo is not None:
        expiry = expiry.astimezone(pytz.timezone("Asia/Shanghai")).replace(tzinfo=None)

    check_time = expiry.replace(hour=1, minute=30, second=0, microsecond=0)
    if check_time < expiry:
        check_time += timedelta(days=1)
    return check_time

def should_alert_expired_login(user_record, emby_user_id: str, login_time: Optional[datetime]) -> Optional[datetime]:
    if not login_time:
        return None

    expiry_record = user_record
    if not expiry_record or not getattr(expiry_record, 'ex', None):
        expiry_record = sql_get_emby2(emby_user_id)

    expiry = getattr(expiry_record, 'ex', None) if expiry_record else None
    if not expiry or getattr(expiry_record, 'lv', None) == 'a':
        return None

    check_time = get_expiry_check_time(expiry)
    if login_time < check_time:
        return None

    now = time.monotonic()
    last_alert = expired_login_alerts.get(emby_user_id)
    if last_alert is not None and now - last_alert < EXPIRED_LOGIN_ALERT_COOLDOWN:
        return None

    expired_login_alerts[emby_user_id] = now
    return check_time

def format_user_level(user_record) -> str:
    if not user_record or not hasattr(user_record, 'lv'):
        return ""
    
    level_map = {
        'a': " (白名单)",
        'b': " (普通用户)",
        'c': " (已封禁)"
    }
    return level_map.get(user_record.lv, " (未知等级)")

def format_user_expiry(user_record, embyid=None) -> str:
    if user_record and getattr(user_record, 'lv', None) == 'a':
        return "+ ∞"

    if user_record and getattr(user_record, 'ex', None):
        return str(user_record.ex)

    if embyid:
        e2 = sql_get_emby2(embyid)
        if e2:
            if getattr(e2, 'lv', None) == 'a':
                return "+ ∞"
            if getattr(e2, 'ex', None):
                return str(e2.ex)

    return "无数据"

async def format_user_info(user_record, fallback_name='未知用户') -> Tuple[str, str]:
    emby_username = fallback_name
    if user_record:
        emby_username = user_record.name

    if user_record and user_record.tg:
        tg_display_name = emby_username
        tg_username = "无数据"
        try:
            chat_info = await bot.get_chat(user_record.tg)
            tg_display_name = chat_info.first_name
            if chat_info.username:
                tg_username = chat_info.username
        except PeerIdInvalid:
            LOGGER.warning(f"无法获取TG用户信息：无效的 Peer ID {user_record.tg}")
        except Exception as e:
            LOGGER.error(f"获取TG用户信息时发生未知错误 (ID: {user_record.tg}): {e}")
            tg_display_name = "无法获取昵称"

        safe_display_name = str(tg_display_name).replace('[', '').replace(']', '')
        tg_info_str = (
            f"   - **昵称:** `{safe_display_name}` (`{user_record.tg}`)\n"
            f"   - **用户名:** `{tg_username}`\n"
            f"   - **深链接:** tg://user?id={user_record.tg}"
        )
        return tg_info_str, emby_username
        
    elif user_record:
        tg_info_str = f"   - **未绑定**"
        return tg_info_str, emby_username
    
    tg_info_str = f"   - **无数据**"
    return tg_info_str, emby_username

async def get_ip_location(ip: str) -> str:
    """获取 IP 定位信息"""
    if not ip or ip == '无数据':
        return ""
    
    if ip in ip_cache:
        return ip_cache[ip].get('location', "")

    url = f"https://geoip.icysn.com/api/json?ip={ip}"
    headers = {
        "Accept-Encoding": "gzip, deflate"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5, headers=headers) as response:
                if response.status == 200:
                    res = await response.json()
                    if res.get('code') == 0 and 'data' in res:
                        data = res['data']
                        parts = []
                        
                        country = data.get('country', {}).get('name')
                        if country: parts.append(country)
                        
                        regions = data.get('regions', [])
                        if regions: parts.extend(regions)
                        
                        isp = data.get('as', {}).get('info')
                        if isp: parts.append(isp)
                        
                        net_type = data.get('type')
                        if net_type: parts.append(net_type)
                        
                        location_str = " ".join(parts)

                        ip_cache[ip] = {
                            'location': location_str,
                            'timestamp': time.time()
                        }
                        return location_str
    except Exception as e:
        LOGGER.error(f"获取 IP 定位失败 ({ip}): {e}")
    
    return ""

# --- 消息构建函数 ---

def build_login_message(date, tg_info_str, emby_username, user_id, session_data, login_host, user_level_str, user_expiry_str, ip_location=""):
    client_name = session_data.get('Client', '无数据')
    client_version = session_data.get('ApplicationVersion', '无数据')
    device_name = session_data.get('DeviceName', '无数据')
    device_id = session_data.get('DeviceId', '无数据')
    remote_ip = session_data.get('RemoteEndPoint', '无数据')

    return (
        f"**🔐 用户登录通知**\n\n"
        f"👤 **用户名称:** `{emby_username}`{user_level_str}\n"
        f"🗓 **到期时间:** `{user_expiry_str}`\n"
        f"🕒 **登录时间:** `{date}`\n"
        f"🆔 **用户 ID:** `{user_id}`\n\n"
        f"📱 **TG 信息:**\n{tg_info_str}\n\n"
        f"💻 **设备信息:**\n"
        f"   - **设备名称:** `{device_name}`\n"
        f"   - **客户端:** `{client_name} ({client_version})`\n"
        f"   - **设备 ID:** `{device_id}`\n\n"
        f"🌐 **网络信息:**\n"
        f"   - **用户 IP:** `{remote_ip}`\n"
        f"   - **IP 信息:** `{ip_location or '无数据'}`\n"
        f"   - **登录线路:** `{login_host}`"
    )


def build_playback_message(date, tg_info_str, emby_username, user_id, item_data, session_data, login_host, user_level_str, user_expiry_str, ip_location=""):
    series_name = item_data.get('SeriesName', '电影')
    episode_name = item_data.get('Name', '无数据')
    media_type = item_data.get('Type', '无数据')
    
    runtime_ticks = item_data.get('RunTimeTicks', 0)
    runtime_minutes = round(runtime_ticks / 10**7 / 60, 1) if runtime_ticks else 0
    
    size_bytes = item_data.get('Size', 0)
    size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else 0
    
    bitrate_bps = item_data.get('Bitrate', 0)
    bitrate_kbps = round(bitrate_bps / 1000) if bitrate_bps else 0
    
    client_name = session_data.get('Client', '无数据')
    client_version = session_data.get('ApplicationVersion', '无数据')
    device_name = session_data.get('DeviceName', '无数据')
    device_id = session_data.get('DeviceId', '无数据')
    remote_ip = session_data.get('RemoteEndPoint', '无数据')

    return (
        f"**📺 用户播放通知**\n\n"
        f"👤 **用户名称:** `{emby_username}`{user_level_str}\n"
        f"🗓 **到期时间:** `{user_expiry_str}`\n"
        f"🕒 **播放时间:** `{date}`\n"
        f"🆔 **用户 ID:** `{user_id}`\n\n"
        f"📱 **TG 信息:**\n{tg_info_str}\n\n"
        f"🎬 **播放内容:**\n"
        f"   - **名称:** `{series_name} - {episode_name}`\n"
        f"   - **类型:** `{media_type}`\n"
        f"   - **时长:** `{runtime_minutes} 分钟`\n"
        f"   - **大小:** `{size_mb} MB`\n"
        f"   - **码率:** `{bitrate_kbps} kbps`\n\n"
        f"💻 **设备信息:**\n"
        f"   - **设备名称:** `{device_name}`\n"
        f"   - **客户端:** `{client_name} ({client_version})`\n"
        f"   - **设备 ID:** `{device_id}`\n\n"
        f"🌐 **网络信息:**\n"
        f"   - **用户 IP:** `{remote_ip}`\n"
        f"   - **IP 信息:** `{ip_location or '无数据'}`\n"
        f"   - **播放线路:** `{login_host}`"
    )

# --- Telegram 交互 ---
async def send_telegram_message(text: str, thread_id: str = None, session_id: str = None, user_name: str = None):
    if not TG_LOG_BOT_TOKEN or not TG_LOG_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TG_LOG_CHAT_ID, 'text': text, 'parse_mode': ParseMode.MARKDOWN.value}
    if thread_id:
        payload['message_thread_id'] = thread_id

    max_attempts = 5
    delay = 1

    for attempt in range(max_attempts):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status == 200:
                        resp_json = await response.json()
                        if resp_json.get('ok') and session_id:
                            message_id = resp_json.get('result', {}).get('message_id')
                            if message_id:
                                play_session_cache[session_id] = {
                                    'message_id': message_id,
                                    'chat_id': TG_LOG_CHAT_ID,
                                    'thread_id': thread_id,
                                    'user_name': user_name,
                                    'timestamp': time.time()
                                }
                                if len(play_session_cache) > PLAY_SESSION_MAX_SIZE:
                                    play_session_cache.popitem(last=False)
                        return
                    else:
                        raise Exception(f"HTTP {response.status} - {await response.text()}")
        except Exception as e:
            LOGGER.error(f"尝试 {attempt+1}/{max_attempts} 发送TG日志失败: {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay + 0.1 * attempt)
                delay *= 2
            else:
                LOGGER.error(f"发送TG日志失败，达到最大重试次数 ({max_attempts})")

async def send_playback_stop_reply(session_id: str, user_name: str):
    cache_entry = play_session_cache.pop(session_id, None)
    if not cache_entry: return
    stop_msg = f"🛑 用户 `{user_name}` 的播放已结束"
    url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': cache_entry['chat_id'], 'text': stop_msg, 'reply_to_message_id': cache_entry['message_id']}
    if cache_entry['thread_id']: payload['message_thread_id'] = cache_entry['thread_id']
    try:
        async with aiohttp.ClientSession() as session: await session.post(url, json=payload, timeout=10)
    except Exception as e: LOGGER.error(f"发送播放停止回复失败: {e}")

# --- Webhook 主路由 ---
@route.post("/webhook", tags=["Emby Webhook"])
async def webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = data.get('Event')
    user_data = data.get('User', {})
    user_name_from_webhook = user_data.get('Name', '未知用户')
    emby_user_id = user_data.get('Id')

    if not emby_user_id or user_name_from_webhook in IGNORED_USERS_SET:
        return Response(status_code=204)

    user_record = sql_get_emby(emby_user_id)
    tg_info_str, emby_username = await format_user_info(user_record, fallback_name=user_name_from_webhook)
    
    user_level_str = format_user_level(user_record)
    user_expiry_str = format_user_expiry(user_record, embyid=emby_user_id)

    login_time = parse_utc_to_beijing(data.get('Date', ''))
    date = login_time.strftime("%Y-%m-%d %H:%M:%S") if login_time else "未知时间"
    session_data = data.get('Session', {})
    session_id = session_data.get('Id')
    device_id = session_data.get('DeviceId', '无数据')
    remote_ip = session_data.get('RemoteEndPoint', '无数据')
    ip_location = await get_ip_location(remote_ip)

    # --- 事件处理分发 ---
    if event == EVENT_USER_AUTHENTICATED:
        await asyncio.sleep(2)
        
        login_host = host_cache.get(device_id, {}).get('host', '无数据')
        if login_host == '无数据':
            login_host = host_cache.get(emby_user_id, {}).get('host', '无数据')

        message_text = build_login_message(date, tg_info_str, emby_username, emby_user_id, session_data, login_host, user_level_str, user_expiry_str, ip_location=ip_location)
        await send_telegram_message(message_text, thread_id=TG_LOGIN_THREAD_ID)

        expiry_check_time = should_alert_expired_login(user_record, emby_user_id, login_time)
        if expiry_check_time:
            owner_message = (
                f"🚨 **到期后仍可登录告警**\n"
                f"⏱ **到期检测应执行时间:** `{expiry_check_time.strftime('%Y-%m-%d %H:%M:%S')}`\n\n"
                f"{message_text}"
            )
            try:
                await bot.send_message(owner, owner_message, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                LOGGER.error(f"向 Owner 发送到期登录告警失败: {e}")

    elif event == EVENT_PLAYBACK_START:
        login_host = host_cache.get(device_id, {}).get('host', '无数据')
        if login_host == '无数据':
            login_host = host_cache.get(emby_user_id, {}).get('host', '无数据')
            
        item_data = data.get('Item', {})
        message_text = build_playback_message(date, tg_info_str, emby_username, emby_user_id, item_data, session_data, login_host, user_level_str, user_expiry_str, ip_location=ip_location)
        await send_telegram_message(message_text, thread_id=TG_PLAY_THREAD_ID, session_id=session_id, user_name=emby_username)

    elif event in (EVENT_PLAYBACK_STOP, EVENT_PLAYBACK_PAUSE, EVENT_SESSION_ENDED):
        if session_id:
            await send_playback_stop_reply(session_id, emby_username)

    return Response(content="ok", status_code=200)
