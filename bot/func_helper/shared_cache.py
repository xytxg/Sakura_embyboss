#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
shared_cache.py - 缓存管理模块
"""

import time
import threading
from collections import OrderedDict
from bot import LOGGER

# --- 共享缓存定义 ---
host_cache = {}
HOST_CACHE_EXPIRATION = 600

play_session_cache = OrderedDict()
PLAY_SESSION_EXPIRATION = 7200
PLAY_SESSION_MAX_SIZE = 500

def _clean_expired_caches_task():
    LOGGER.info("🚀 共享缓存清理线程已启动")
    
    while True:
        try:
            time.sleep(60)
            now = time.time()

            expired_host_keys = [
                key for key, data in list(host_cache.items())
                if now - data.get('timestamp', 0) > HOST_CACHE_EXPIRATION
            ]
            if expired_host_keys:
                for key in expired_host_keys:
                    host_cache.pop(key, None)
            
            expired_session_keys = [
                key for key, data in list(play_session_cache.items())
                if now - data.get('timestamp', 0) > PLAY_SESSION_EXPIRATION
            ]
            if expired_session_keys:
                for key in expired_session_keys:
                    play_session_cache.pop(key, None)

        except Exception as e:
            error_info = f"{type(e).__name__}: {e}"
            LOGGER.critical(
                f"FATAL: 共享缓存清理线程发生严重错误，已停止！"
                f"请立即检查并重启服务以防内存泄漏。错误详情: {error_info}"
            )
            break

cleaner_thread = threading.Thread(target=_clean_expired_caches_task, daemon=True)
cleaner_thread.start()
