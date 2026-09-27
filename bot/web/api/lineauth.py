#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
auth.py - Emby 线路鉴权网关
"""
import re
import time
import aiohttp
from pyrogram.enums import ParseMode
from fastapi import APIRouter, Request, Response
from cacheout import Cache
from bot.func_helper.emby import emby
from bot.func_helper.shared_cache import host_cache
from bot import LOGGER, group, bot, owner, api as config_api, config
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby

route = APIRouter()

# --- 应用配置 ---
TG_LOG_BOT_TOKEN = config_api.log_to_tg.bot_token
TG_LOG_CHAT_ID = config_api.log_to_tg.chat_id
TG_LOGIN_THREAD_ID = config_api.log_to_tg.login_thread_id
EMBY_WHITE_LIST_HOSTS = config_api.emby_whitelist_line_host
AUTH_COOLDOWN_SECONDS = 300
auth_cache = Cache(maxsize=2000, ttl=AUTH_COOLDOWN_SECONDS)


def is_ban_del_to_owner() -> bool:
    """获取是否将封禁与删除通知仅发给 Owner 的配置"""
    if getattr(config, 'ban_del_to_owner', False):
        return True
    if hasattr(config_api, 'log_to_tg') and getattr(config_api.log_to_tg, 'ban_del_to_owner', False):
        return True
    return False


# --- 日志发送辅助函数 ---
async def send_log_message(message_text: str):
    """发送日志至配置的 TG 日志群/频道/Topic"""
    if not all([TG_LOG_BOT_TOKEN, TG_LOG_CHAT_ID]):
        return

    url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_LOG_CHAT_ID,
        "text": message_text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if TG_LOGIN_THREAD_ID:
        payload["message_thread_id"] = TG_LOGIN_THREAD_ID

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=10) as response:
                if response.status != 200:
                    LOGGER.error(f"发送TG日志失败: {response.status} - {await response.text()}")
    except Exception as e:
        LOGGER.error(f"发送TG日志时发生网络错误: {e}")


async def send_log_bot_to_owner(message_text: str):
    if TG_LOG_BOT_TOKEN and owner:
        url = f"https://api.telegram.org/bot{TG_LOG_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": owner,
            "text": message_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status == 200:
                        return True
                    LOGGER.error(f"日志Bot发送至Owner失败: {response.status} - {await response.text()}")
        except Exception as e:
            LOGGER.error(f"日志Bot发送至Owner异常: {e}")

    try:
        await bot.send_message(owner, message_text, parse_mode=ParseMode.MARKDOWN)
        return True
    except Exception as e:
        LOGGER.error(f"向 Owner 发送通知失败: {e}")
        return False


# --- 统一请求处理路由 ---
@route.api_route("/{path:path}", methods=["GET", "POST", "HEAD", "OPTIONS"])
async def handle_auth_request(request: Request):

    if request.method != "GET":
        return Response(content="True", status_code=200, media_type="text/plain")

    full_path = str(request.url)
    request_host = request.headers.get('host')

    user_id_match = re.search(r'Users/([a-fA-F0-9]{32})', full_path, re.IGNORECASE)

    if user_id_match and request_host:
        emby_user_id = user_id_match.group(1)
        host_cache.set(emby_user_id, {'host': request_host, 'timestamp': time.time()})

    if not user_id_match:
        return Response(content="True", status_code=200, media_type="text/plain")

    user_id = user_id_match.group(1)

    cache_key = (user_id, request_host)
    cached_auth = auth_cache.get(cache_key)

    if cached_auth is not None:
        return Response(content="True" if cached_auth['allowed'] else "False",
                        status_code=200 if cached_auth['allowed'] else 401,
                        media_type="text/plain")

    user_record = sql_get_emby(user_id)

    if not user_record:
        return Response(content="True", status_code=200, media_type="text/plain")

    user_level = user_record.lv

    if user_level == 'a':
        auth_cache.set(cache_key, {'allowed': True})
        return Response(content="True", status_code=200, media_type="text/plain")

    if user_level == 'b':
        if request_host and request_host in EMBY_WHITE_LIST_HOSTS:
            LOGGER.warning(f"🚨 用户 {user_record.name} ({user_record.tg}) 使用了封禁 Host '{request_host}'，触发封禁逻辑！请求内容: {full_path}")
            auth_cache.set(cache_key, {'allowed': False})

            ban_success = await emby.emby_change_policy(id=user_id, method=True)

            owner_message_content = (
                f"👤 **用户**: [{user_record.name}](tg://user?id={user_record.tg}) - `{user_record.tg}`\n"
                f"📌 **违规 Host**: `{request_host}`\n"
                f"🔗 **请求内容**: `{full_path}`\n"
            )

            ban_to_owner = is_ban_del_to_owner()

            if ban_success:
                sql_update_emby(Emby.embyid == user_id, lv='c')

                owner_message = (
                    f"✅ **自动封禁通知** ✅\n\n"
                    f"{owner_message_content}"
                    f"ℹ️ **状态**: 已自动封禁"
                )
                await send_log_bot_to_owner(owner_message)
                await send_log_message(owner_message)

                group_message = (
                    f"🚨 **自动封禁通知** 🚨\n\n"
                    f"👤 用户: [{user_record.name}](tg://user?id={user_record.tg}) - `{user_record.tg}`\n"
                    f"⛔️ 状态: 已自动封禁\n\n"
                    f"📌 原因: 检测到非授权请求\n"
                    f"‼️ 如有疑问，请联系管理员处理"
                )
                try:
                    if ban_to_owner:
                        if user_record.tg:
                            await bot.send_message(user_record.tg, group_message, parse_mode=ParseMode.MARKDOWN)
                    else:
                        sent_message = await bot.send_message(group[0], group_message, parse_mode=ParseMode.MARKDOWN)
                        if user_record.tg:
                            await sent_message.forward(user_record.tg)
                except Exception as e:
                    LOGGER.error(f"发送 Telegram 封禁通知失败: {e}")
            else:
                LOGGER.error(f"通过 Emby API 封禁用户 {user_record.name} ({user_record.tg}) 失败！请手动处理")

                owner_message = (
                    f"🔥 **封禁失败警告** 🔥\n\n"
                    f"{owner_message_content}"
                    f"‼️ **处置**: API调用失败，**请立即手动检查并封禁该用户！**"
                )
                await send_log_bot_to_owner(owner_message)
                await send_log_message(owner_message)

                if not ban_to_owner:
                    group_message = (
                        f"🔥 **封禁失败警告** 🔥\n\n"
                        f"👤 用户: [{user_record.name}](tg://user?id={user_record.tg}) - `{user_record.tg}`\n"
                        f"⛔️ 状态: 自动封禁失败！\n\n"
                        f"‼️ **请立即手动检查并封禁该用户！**"
                    )
                    try:
                        await bot.send_message(group[0], group_message, parse_mode=ParseMode.MARKDOWN)
                    except Exception as e:
                        LOGGER.error(f"向群组发送封禁失败通知失败: {e}")

            return Response(content="False", status_code=401, media_type="text/plain")
        else:
            auth_cache.set(cache_key, {'allowed': True})
            return Response(content="True", status_code=200, media_type="text/plain")

    auth_cache.set(cache_key, {'allowed': False})
    return Response(content="False", status_code=401, media_type="text/plain")
