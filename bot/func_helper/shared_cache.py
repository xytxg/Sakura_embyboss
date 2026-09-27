#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
shared_cache.py - 共享缓存管理模块与到期通知 Hook
"""

from cacheout import Cache
from bot import LOGGER, owner, config

# --- 共享缓存定义 (基于 Cache，支持自动 TTL 与 LRU 淘汰) ---
HOST_CACHE_EXPIRATION = 600
host_cache = Cache(maxsize=2000, ttl=HOST_CACHE_EXPIRATION)

PLAY_SESSION_EXPIRATION = 7200
PLAY_SESSION_MAX_SIZE = 500
play_session_cache = Cache(maxsize=PLAY_SESSION_MAX_SIZE, ttl=PLAY_SESSION_EXPIRATION)

IP_CACHE_EXPIRATION = 3600
ip_cache = Cache(maxsize=2000, ttl=IP_CACHE_EXPIRATION)


def _install_check_expired_hook():
    try:
        import bot.scheduler.check_ex as check_ex
        _orig_check_expired = check_ex.check_expired

        async def _hooked_check_expired():
            ban_del_to_owner = getattr(config, 'ban_del_to_owner', False)
            if not ban_del_to_owner and hasattr(config, 'api') and hasattr(config.api, 'log_to_tg'):
                ban_del_to_owner = getattr(config.api.log_to_tg, 'ban_del_to_owner', False)

            if ban_del_to_owner:
                orig_group = check_ex.group
                try:
                    check_ex.group = [owner]
                    return await _orig_check_expired()
                finally:
                    check_ex.group = orig_group
            return await _orig_check_expired()

        check_ex.check_expired = _hooked_check_expired

        import sys
        if 'bot.modules.panel.sched_panel' in sys.modules:
            sp = sys.modules['bot.modules.panel.sched_panel']
            if hasattr(sp, 'sched_dict') and 'check_ex' in sp.sched_dict:
                sp.sched_dict['check_ex'] = _hooked_check_expired
            if hasattr(sp, 'check_expired'):
                sp.check_expired = _hooked_check_expired
        LOGGER.info("✅ 已成功装载到期封禁/删除通知重定向 Hook")
    except Exception as e:
        LOGGER.warning(f"装载 check_expired Hook 异常: {e}")


_install_check_expired_hook()
