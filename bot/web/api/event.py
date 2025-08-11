#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
event.py - Emby Webhook 事件处理
"""
import time
import pytz
import aiohttp
from typing import Tuple
from datetime import datetime
from pyrogram.enums import ParseMode
from pyrogram.errors import PeerIdInvalid
from bot import LOGGER, bot, api as config_api
from bot.sql_helper.sql_emby import sql_get_emby
from fastapi import APIRouter, Request, Response, HTTPException
from bot.func_helper.shared_cache import host_cache, play_session_cache, PLAY_SESSION_MAX_SIZE

route = APIRouter(  )

# --- 配置加载 ---
TG_LOG_BOT_TOKEN = config_api.log_to_tg.bot_token
TG_LOG_CHAT_ID = config_api.log_to_tg.chat_id
TG_LOGIN_THREAD_ID = config_api.log_to_tg.login_thread_id
TG_PLAY_THREAD_ID = config_api.log_to_tg.play_thread_id
IGNORED_USERS_SET = {user.strip().lower() for user in getattr(config_api, 'emby_webhook', {}).get('ignore_users', [])}

# --- 事件常量 ---
EVENT_USER_AUTHENTICATED = 'user.authenticated'
EVENT_PLAYBACK_START = 'playback.start'
EVENT_PLAYBACK_STOP = 'playback.stop'
EVENT_PLAYBACK_PAUSE = 'playback.pause'
EVENT_SESSION_ENDED = 'playback.sessionended'

# --- 工具函数 ---

def convert_utc_to_beijing(utc_str: str) -> str:
    try:
        utc_time = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return utc_time.astimezone(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"

def format_user_level(user_record) -> str:
    if not user_record or not hasattr(user_record, 'lv'):
        return "未入库"
    
    level_map = {
        'a': "白名单",
        'b': "普通用户",
        'c': "已封禁"
    }
    return level_map.get(user_record.lv, "未知等级")

async def format_user_info(user_record, fallback_name='未知用户') -> Tuple[str, str]:
    emby_username = fallback_name
    if user_record:
        emby_username = user_record.name

    if user_record and user_record.tg:
        tg_display_name = emby_username 
        try:
            chat_info = await bot.get_chat(user_record.tg)
            tg_display_name = chat_info.username if chat_info.username else chat_info.first_name
        except PeerIdInvalid:
            LOGGER.warning(f"无法获取TG用户信息：无效的 Peer ID {user_record.tg}。")
        except Exception as e:
            LOGGER.error(f"获取TG用户信息时发生未知错误 (ID: {user_record.tg}): {e}")
            tg_display_name = "无法获取昵称"

        safe_display_name = str(tg_display_name).replace('[', '').replace(']', '')
        tg_info_str = f"[{safe_display_name}](tg://user?id={user_record.tg}) - `{user_record.tg}`"
        return tg_info_str, emby_username
        
    elif user_record:
        tg_info_str = f"`{emby_username}` (TG未绑定)"
        return tg_info_str, emby_username
    
    tg_info_str = f"`{emby_username}` (用户未入库)"
    return tg_info_str, emby_username

# --- 消息构建函数 ---

def build_login_message(date, tg_info_str, emby_username, user_id, session_data, login_host, user_level_str):
    client_name = session_data.get('Client', '未知')
    client_version = session_data.get('ApplicationVersion', '未知')
    device_name = session_data.get('DeviceName', '未知')
    device_id = session_data.get('DeviceId', '未知设备ID')
    remote_ip = session_data.get('RemoteEndPoint', '未知')

    return (
        f"**🔐 用户登录通知**\n\n"
        f"👤 **Emby 用户:** `{emby_username}`\n - `{user_level_str}`"
        f"🆔 **用户 ID:** `{user_id}`\n"
        f"📱 **TG 账户:** {tg_info_str}\n"
        f"🕒 **时间:** `{date}`\n\n"
        f"💻 **设备信息:**\n"
        f"   - **设备名称:** `{device_name}`\n"
        f"   - **客户端:** `{client_name} ({client_version})`\n"
        f"   - **设备 ID:** `{device_id}`\n\n"
        f"🌐 **网络信息:**\n"
        f"   - **用户 IP:** `{remote_ip}`\n"
        f"   - **登录线路:** `{login_host}`"
    )

def build_playback_message(date, tg_info_str, emby_username, user_id, item_data, session_data, login_host, user_level_str):
    series_name = item_data.get('SeriesName', '电影')
    episode_name = item_data.get('Name', '未知')
    media_type = item_data.get('Type', '未知类型')
    
    runtime_ticks = item_data.get('RunTimeTicks', 0)
    runtime_minutes = round(runtime_ticks / 10**7 / 60, 1) if runtime_ticks else 0
    
    size_bytes = item_data.get('Size', 0)
    size_mb = round(size_bytes / (1024 * 1024), 2) if size_bytes else 0
    
    bitrate_bps = item_data.get('Bitrate', 0)
    bitrate_kbps = round(bitrate_bps / 1000) if bitrate_bps else 0
    
    client_name = session_data.get('Client', '未知')
    client_version = session_data.get('ApplicationVersion', '未知')
    device_name = session_data.get('DeviceName', '未知')
    device_id = session_data.get('DeviceId', '未知设备ID')
    remote_ip = session_data.get('RemoteEndPoint', '未知')

    return (
        f"**📺 用户播放通知**\n\n"
        f"👤 **Emby 用户:** `{emby_username}` - `{user_level_str}`\n"
        f"🆔 **用户 ID:** `{user_id}`\n"
        f"📱 **TG 账户:** {tg_info_str}\n"
        f"🕒 **时间:** `{date}`\n\n"
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
        f"   - **播放线路:** `{login_host}`"
    )

# --- Telegram 交互 ---
async def send_telegram_message(text: str, thread_id: str = None, session_id: str = None, user_name: str = None):
    if not TG_LOG_BOT_TOKEN or not TG_LOG_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': TG_LOG_CHAT_ID, 'text': text, 'parse_mode': ParseMode.MARKDOWN.value}
    if thread_id: payload['message_thread_id'] = thread_id
    try:
        async with aiohttp.ClientSession(  ) as session:
            async with session.post(url, json=payload, timeout=10) as response:
                if response.status == 200:
                    resp_json = await response.json()
                    if resp_json.get('ok') and session_id:
                        message_id = resp_json.get('result', {}).get('message_id')
                        if message_id:
                            play_session_cache[session_id] = {'message_id': message_id, 'chat_id': TG_LOG_CHAT_ID, 'thread_id': thread_id, 'user_name': user_name, 'timestamp': time.time()}
                            if len(play_session_cache) > PLAY_SESSION_MAX_SIZE: play_session_cache.popitem(last=False)
                else: LOGGER.error(f"发送TG日志失败: {response.status} - {await response.text()}")
    except Exception as e: LOGGER.error(f"发送TG日志时发生网络错误: {e}")

async def send_playback_stop_reply(session_id: str, user_name: str):
    cache_entry = play_session_cache.pop(session_id, None)
    if not cache_entry: return
    stop_msg = f"🛑 用户 `{user_name}` 的播放已结束。"
    url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
    payload = {'chat_id': cache_entry['chat_id'], 'text': stop_msg, 'reply_to_message_id': cache_entry['message_id']}
    if cache_entry['thread_id']: payload['message_thread_id'] = cache_entry['thread_id']
    try:
        async with aiohttp.ClientSession(  ) as session: await session.post(url, json=payload, timeout=10)
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

    if not emby_user_id or user_name_from_webhook.lower() in IGNORED_USERS_SET:
        return Response(status_code=204)

    user_record = sql_get_emby(emby_user_id)
    tg_info_str, emby_username = await format_user_info(user_record, fallback_name=user_name_from_webhook)
    
    user_level_str = format_user_level(user_record)

    date = convert_utc_to_beijing(data.get('Date', ''))
    session_data = data.get('Session', {})
    session_id = session_data.get('Id')
    device_id = session_data.get('DeviceId', '未知设备ID')
    
    login_host = host_cache.get(device_id, {}).get('host', '未知')
    if login_host == '未知':
        login_host = host_cache.get(emby_user_id, {}).get('host', '未知')


    # --- 事件处理分发 ---
    if event == EVENT_USER_AUTHENTICATED:
        message_text = build_login_message(date, tg_info_str, emby_username, emby_user_id, session_data, login_host, user_level_str)
        await send_telegram_message(message_text, thread_id=TG_LOGIN_THREAD_ID)

    elif event == EVENT_PLAYBACK_START:
        item_data = data.get('Item', {})
        message_text = build_playback_message(date, tg_info_str, emby_username, emby_user_id, item_data, session_data, login_host, user_level_str)
        await send_telegram_message(message_text, thread_id=TG_PLAY_THREAD_ID, session_id=session_id, user_name=emby_username)

    elif event in (EVENT_PLAYBACK_STOP, EVENT_PLAYBACK_PAUSE, EVENT_SESSION_ENDED):
        if session_id:
            await send_playback_stop_reply(session_id, emby_username)

    return Response(content="ok", status_code=200)
