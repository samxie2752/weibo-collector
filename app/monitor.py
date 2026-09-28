#!/usr/bin/env python3
"""账号监控：后台定时检查已采集账号的时间线，新微博自动入库。

每个账号一个守护线程，每轮用增量模式采集器（连续 2 页无新内容即追平），
只抓公开时间线头部。间隔下限 60 秒——更高的频率会触发微博风控。
Cookie 持久化在 data/{uid}/cookie.txt（data/ 已 gitignore），服务重启后自动恢复监控。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

from . import crawler
from . import events as events_mod
from . import media as media_mod
from .store import DATA_DIR, Store

log = logging.getLogger("weibo.monitor")

MIN_INTERVAL = 60          # 秒，服务端强制下限
SEEN_STOP_PAGES = 2        # 连续 N 页无新内容视为已追平


def cookie_path(uid: str) -> str:
    return os.path.join(DATA_DIR, str(uid), "cookie.txt")


def check_once(uid: str, cookie: str, store: Optional[Store] = None,
               collector: Optional[crawler.WeiboCollector] = None) -> List[Dict]:
    """单轮检查：增量抓取新微博入库，返回新增微博明细列表。

    store/collector 可注入以便离线测试；默认自行构造。
    """
    own = store is None
    store = store or Store(uid)
    try:
        if collector is None:
            collector = crawler.WeiboCollector(uid, cookie,
                                               stop_on_seen_pages=SEEN_STOP_PAGES)
            collector.seen_ids = {
                r[0] for r in store._conn.execute(
                    "SELECT bid FROM weibo WHERE user_id=?",
                    (uid,)).fetchall()}
        fresh: List[Dict] = []
        for wb in collector.iter_weibos():
            store.save_weibo(wb)
            fresh.append({"bid": wb["bid"], "created_at": wb["created_at"],
                          "text": wb["text"]})
        return fresh
    finally:
        if own:
            store.close()


class MonitorState:
    def __init__(self, uid: str, interval: int):
        self.uid = uid
        self.interval = interval
        self.running = True
        self.paused = False        # 采集任务运行期间暂停检查，避免请求叠加
        self.status = "running"    # running / captcha / error
        self.last_check = ""
        self.last_new = 0
        self.total_new = 0
        self.error = ""

    def info(self) -> Dict:
        return {
            "uid": self.uid, "interval": self.interval,
            "status": self.status, "running": self.running,
            "last_check": self.last_check, "last_new": self.last_new,
            "total_new": self.total_new, "error": self.error,
        }


class MonitorManager:
    def __init__(self):
        self.states: Dict[str, MonitorState] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ---------- 对外接口 ----------

    def start(self, uid: str, interval: int, cookie: str = "") -> MonitorState:
        interval = max(MIN_INTERVAL, int(interval))
        if cookie.strip():
            self._save_cookie(uid, cookie.strip())
        elif not os.path.exists(cookie_path(uid)):
            raise ValueError("需要提供 Cookie（或该账号此前已保存过）")
        with self._lock:
            old = self.states.get(uid)
            if old:
                old.running = False
            st = MonitorState(uid, interval)
            self.states[uid] = st
            t = threading.Thread(target=self._loop, args=(st,), daemon=True,
                                 name=f"monitor-{uid}")
            self._threads[uid] = t
            t.start()
        s = Store(uid)
        s.set_meta("monitor_interval", str(interval))
        s.close()
        return st

    def stop(self, uid: str):
        with self._lock:
            st = self.states.pop(uid, None)
        if st:
            st.running = False
            log.info("monitor %s stopped", uid)

    def info(self, uid: str) -> Optional[Dict]:
        st = self.states.get(uid)
        return st.info() if st else None

    def pause(self, uid: str):
        st = self.states.get(uid)
        if st:
            st.paused = True

    def unpause(self, uid: str):
        st = self.states.get(uid)
        if st:
            st.paused = False

    def auto_resume(self):
        """服务启动时恢复监控（有 cookie.txt 且 meta 里有 interval 的账号）。"""
        if not os.path.isdir(DATA_DIR):
            return
        for uid in os.listdir(DATA_DIR):
            if not os.path.exists(cookie_path(uid)):
                continue
            try:
                s = Store(uid)
                interval = s.get_meta("monitor_interval")
                s.close()
                if interval.isdigit():
                    st = self.start(uid, int(interval))
                    log.info("monitor %s resumed (every %ss)", uid, st.interval)
            except Exception as e:
                log.warning("monitor %s resume failed: %s", uid, e)

    # ---------- 内部 ----------

    def _load_cookie(self, uid: str) -> str:
        try:
            with open(cookie_path(uid), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_cookie(self, uid: str, cookie: str):
        os.makedirs(os.path.dirname(cookie_path(uid)), exist_ok=True)
        with open(cookie_path(uid), "w", encoding="utf-8") as f:
            f.write(cookie)

    def _loop(self, st: MonitorState):
        while st.running:
            # 被采集任务暂停期间不发起任何请求
            while st.paused and st.running:
                time.sleep(2)
            if not st.running:
                return
            try:
                cookie = self._load_cookie(st.uid)
                if not cookie:
                    st.status = "error"
                    st.error = "Cookie 丢失，请重新开启监控并填写 Cookie"
                    break
                st.last_check = datetime.now().strftime("%m-%d %H:%M:%S")
                fresh = check_once(st.uid, cookie)
                st.last_new = len(fresh)
                st.total_new += len(fresh)
                st.status = "running"
                st.error = ""
                st.backoff_until = 0.0  # 成功即解除退避
                if fresh:
                    log.info("monitor %s: +%d new weibos", st.uid, len(fresh))
                    # 事件驱动：快推 + insight 分析联动（异常不影响监控主流程）
                    try:
                        events_mod.dispatch_new_weibos(
                            Store(st.uid), st.uid, fresh)
                    except Exception as e:
                        log.warning("dispatch events: %s", e)
                    # 全自动备份闭环：新微博入库后自动补下载其图片
                    media_mod.manager.start(st.uid)
            except crawler.NeedCaptchaError as e:
                # 60s 轮询的自动退避：风控信号 → 180s 持续 30 分钟后恢复
                now = time.time()
                if now - getattr(st, "backoff_until", 0.0) > 0:
                    st.interval_slow = 180
                    st.backoff_until = now + 1800
                    log.warning("monitor %s 风控，退避 180s×30min", st.uid)
                st.status = "captcha_cooldown"
                st.error = f"风控退避中（180s）: {e.url[:80]}"
            except Exception as e:
                st.status = "error"
                st.error = f"{type(e).__name__}: {e}"
                log.warning("monitor %s error: %s", st.uid, e)
            # 分片睡眠；风控退避期间用 180s
            sleep_s = st.interval
            if getattr(st, "backoff_until", 0.0) > time.time():
                sleep_s = 180
            for _ in range(0, sleep_s, 2):
                if not st.running:
                    return
                time.sleep(2)


manager = MonitorManager()
