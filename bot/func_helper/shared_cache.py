#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
shared_cache.py - 共享缓存管理模块与到期通知 Hook
"""

from cacheout import Cache
from bot import bot, LOGGER, owner, config


class DictCache(Cache):
    """
    兼具 dict 语法兼容性与 Cacheout 自动 TTL / LRU 淘汰特性的缓存类。
    支持 cache[k] = v, cache[k], k in cache, cache.pop(k, default) 等字典操作。
    """
    def __setitem__(self, key, value):
        self.set(key, value)

    def __getitem__(self, key):
        if not self.has(key):
            raise KeyError(key)
        return self.get(key)

    def __contains__(self, key):
        return self.has(key)

    def __delitem__(self, key):
        if not self.delete(key):
            raise KeyError(key)

    def pop(self, key, default=None):
        if self.has(key):
            val = self.get(key)
            self.delete(key)
            return val
        return default


# --- 共享缓存定义 (基于 DictCache，全面兼容字典语法与自动淘汰) ---
HOST_CACHE_EXPIRATION = 600
host_cache = DictCache(maxsize=2000, ttl=HOST_CACHE_EXPIRATION)

PLAY_SESSION_EXPIRATION = 7200
PLAY_SESSION_MAX_SIZE = 500
play_session_cache = DictCache(maxsize=PLAY_SESSION_MAX_SIZE, ttl=PLAY_SESSION_EXPIRATION)

IP_CACHE_EXPIRATION = 3600
ip_cache = DictCache(maxsize=2000, ttl=IP_CACHE_EXPIRATION)


def _install_check_expired_hook():
    try:
        import bot.scheduler.check_ex as check_ex
        import bot as bot_module
        _orig_check_expired = check_ex.check_expired

        async def _hooked_check_expired():
            val = getattr(config, 'ban_del_to_owner', False)
            if not val and hasattr(config, 'api') and hasattr(config.api, 'log_to_tg'):
                val = getattr(config.api.log_to_tg, 'ban_del_to_owner', False)
            ban_del_to_owner = val.lower() in ('true', '1', 'yes') if isinstance(val, str) else bool(val)

            orig_group = getattr(check_ex, 'group', None)
            orig_bot_group = getattr(bot_module, 'group', None)
            orig_send_message = bot.send_message
            target_chat = owner if ban_del_to_owner else (orig_group[0] if orig_group else owner)

            async def _safe_send_message(chat_id, *args, **kwargs):
                try:
                    return await orig_send_message(chat_id, *args, **kwargs)
                except Exception as ex:
                    if target_chat and chat_id != target_chat:
                        msg_text = str(args[0] if args else kwargs.get('text', ''))
                        try:
                            await orig_send_message(
                                target_chat,
                                f"⚠️ [用户未开启Bot私信，无法送达]\n\n{msg_text}"
                            )
                        except Exception:
                            pass
                    raise ex

            try:
                if ban_del_to_owner:
                    check_ex.group = [owner]
                    bot_module.group = [owner]
                bot.send_message = _safe_send_message
                return await _orig_check_expired()
            finally:
                if orig_group is not None:
                    check_ex.group = orig_group
                if orig_bot_group is not None:
                    bot_module.group = orig_bot_group
                bot.send_message = orig_send_message

        _hooked_check_expired.__name__ = 'check_expired'
        check_ex.check_expired = _hooked_check_expired

        import sys
        if 'bot.modules.panel.sched_panel' in sys.modules:
            sp = sys.modules['bot.modules.panel.sched_panel']
            if hasattr(sp, 'action_dict') and 'check_ex' in sp.action_dict:
                sp.action_dict['check_ex'] = _hooked_check_expired
            if hasattr(sp, 'check_expired'):
                sp.check_expired = _hooked_check_expired

        import bot.scheduler as bs
        bs.check_expired = _hooked_check_expired

        try:
            from bot.func_helper.scheduler import scheduler
            if hasattr(scheduler, 'SCHEDULER') and scheduler.SCHEDULER.get_job('check_expired'):
                scheduler.remove_job('check_expired')
                scheduler.add_job(_hooked_check_expired, 'cron', hour=1, minute=30, id='check_expired')
        except Exception:
            pass

        LOGGER.info("✅ 已成功装载到期封禁/删除通知重定向及私聊容灾 Hook")
    except Exception as e:
        LOGGER.warning(f"装载 check_expired Hook 异常: {e}")


def _install_emby_del_hook():
    """
    Hook emby.emby_del:
    当删除 Emby 用户因服务端 404 (资源不存在) 失败时，说明该用户早前已在 Emby 控制台被删除。
    此时判定为删除成功，允许系统清理数据库过期残留记录，避免每天到期检测重复报错。
    """
    try:
        from bot.func_helper.emby import emby
        _orig_emby_del = emby.emby_del

        async def _safe_emby_del(emby_id=None, user_id=None, **kwargs):
            target_id = emby_id or user_id or kwargs.get('id')
            success = await _orig_emby_del(emby_id=target_id, **kwargs)
            if not success and target_id:
                user_info = await emby.user(target_id)
                if not user_info:
                    LOGGER.info(f"Emby 服务端用户 {target_id} 实际已不存在，视为删除成功")
                    return True
            return success

        emby.emby_del = _safe_emby_del
        LOGGER.info("✅ 已成功装载 emby_del 容错清理 Hook")
    except Exception as e:
        LOGGER.warning(f"装载 emby_del Hook 异常: {e}")


_install_check_expired_hook()
_install_emby_del_hook()
