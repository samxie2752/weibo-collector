#!/usr/bin/env python3
"""采集任务管理：单线程串行队列 + 状态机。

状态：queued → running → done / failed
                  └→ need_captcha →（用户过验证码后 resume）→ running
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from . import crawler
from . import media as media_mod
from . import monitor as monitor_mod
from .store import Store

log = logging.getLogger("weibo.tasks")

LOG_KEEP = 300  # 每个任务保留的日志行数

BAN_DECAY_SECONDS = 1800  # 风控计数衰减：每过 30 分钟宽恕一级


def effective_ban_count(ban_count: int, last_ban_at: str) -> int:
    """带时间衰减的风控计数：距上次触发风控每满 30 分钟降一级。

    没有时间戳的历史记录视为久远（清零）——避免旧节奏攒下的次数一直惩罚当下。
    """
    if ban_count <= 0:
        return 0
    if not last_ban_at.isdigit():
        return 0
    elapsed = time.time() - int(last_ban_at)
    return max(0, ban_count - int(elapsed // BAN_DECAY_SECONDS))


def resume_cooldown(ban_count: int) -> int:
    """近期触发风控的次数 → 恢复采集前的冷却秒数（指数递增，封顶 15 分钟）。

    1 次=2 分钟，2 次=4 分钟，3 次=8 分钟……频繁触发说明需要换 Cookie，
    冷却用来打破"过完验证马上全速重跑→立刻再被封"的循环。
    """
    if ban_count <= 0:
        return 0
    return min(120 * 2 ** (ban_count - 1), 900)


class Task:
    def __init__(self, user_id: str, cookie: str):
        self.id = uuid.uuid4().hex[:12]
        self.user_id = user_id
        self.cookie = cookie
        self.state = "queued"      # queued/running/need_captcha/done/failed
        self.error = ""
        self.captcha_url = ""
        self.page = 0
        self.collected = 0
        self.total = 0             # 用户资料里的微博总数
        self.screen_name = ""
        self.logs: List[str] = []
        self.action = ""          # 当前动作（页面实时显示）
        self.action_until = 0     # 动作截止时间戳（冷却/休息时的倒计时），0 表示无
        self.created_at = datetime.now().strftime("%m-%d %H:%M:%S")
        self.finished_at = ""

    def info(self) -> Dict:
        return {
            "id": self.id, "user_id": self.user_id,
            "screen_name": self.screen_name,
            "state": self.state, "error": self.error,
            "captcha_url": self.captcha_url,
            "page": self.page, "collected": self.collected, "total": self.total,
            "action": self.action, "action_until": self.action_until,
            "progress": (min(99, round(self.collected / self.total * 100))
                         if self.total and self.state == "running" else
                         (100 if self.state == "done" else 0)),
            "created_at": self.created_at, "finished_at": self.finished_at,
            "logs": self.logs[-50:],
        }


class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self._queue: "queue.Queue[Task]" = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    # ---------- 对外接口 ----------

    def create(self, user_id: str, cookie: str) -> Task:
        task = Task(user_id, cookie)
        with self._lock:
            self.tasks[task.id] = task
        self._queue.put(task)
        task.logs.append(f"[{task.created_at}] 任务创建，排队中")
        return task

    def resume(self, task_id: str, cookie: str = "") -> Optional[Task]:
        task = self.tasks.get(task_id)
        if not task or task.state != "need_captcha":
            return None
        if cookie.strip():
            task.cookie = cookie.strip()
        task.state = "queued"
        task.captcha_url = ""
        task.logs.append(f"[{datetime.now():%m-%d %H:%M:%S}] 用户已处理验证，重新入队")
        self._queue.put(task)
        return task

    def list(self) -> List[Dict]:
        with self._lock:
            tasks = list(self.tasks.values())
        # 新任务在前
        return [t.info() for t in reversed(tasks)]

    def get(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def running_task_for(self, user_id: str) -> Optional[Task]:
        """该账号是否有排队/进行中的采集任务（防止重复提交）。"""
        for t in self.tasks.values():
            if t.user_id == user_id and t.state in ("queued", "running"):
                return t
        return None

    # ---------- 工作线程 ----------

    def _run_worker(self):
        while True:
            task = self._queue.get()
            self._execute(task)

    def _log(self, task: Task, msg: str):
        line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
        task.logs.append(line)
        if len(task.logs) > LOG_KEEP:
            del task.logs[:-LOG_KEEP]
        log.info("task %s(%s): %s", task.id, task.user_id, msg)

    def _execute(self, task: Task):
        task.state = "running"
        task.error = ""
        started_at = time.time()
        # 采集期间暂停该账号的监控与媒体下载，避免多路请求叠加触发风控
        monitor_mod.manager.pause(task.user_id)
        media_mod.manager.pause(task.user_id)
        self._log(task, f"开始采集用户 {task.user_id}")
        store = None
        try:
            store = Store(task.user_id)
            # 风控冷却：按衰减后的计数决定（历史账随时间自动清账）
            raw_bans = int(store.get_meta("ban_count") or 0)
            ban_count = effective_ban_count(raw_bans,
                                            store.get_meta("last_ban_at"))
            if ban_count:
                cooldown = resume_cooldown(ban_count)
                if ban_count < raw_bans:
                    self._log(task, f"历史风控 {raw_bans} 次已随时间衰减为 "
                                    f"{ban_count} 次")
                self._log(task, f"该账号近期触发过 {ban_count} 次风控，"
                                f"冷却 {cooldown} 秒后开始采集")
                task.action = "风控冷却中"
                task.action_until = time.time() + cooldown
                time.sleep(cooldown)
            task.action, task.action_until = "获取用户资料", 0

            # 断点续采：读取库内进度，决定从第几页开始、以及结束策略
            start_page, stop_on_seen = 1, 0
            task.collected = store.count_weibos()
            if task.collected:
                last_page = store.get_meta("last_page")
                completed = store.get_meta("completed") == "1"
                self._log(task, f"库中已有 {task.collected} 条"
                                f"（上次采到第 {last_page or '?'} 页，"
                                f"{'已采完' if completed else '未采完'}）")
                if completed:
                    # 增量模式：从第 1 页追新微博，连续 3 页无新内容即追平
                    stop_on_seen = 3
                elif last_page.isdigit():
                    # 续采模式：从中断页的前一页开始（容忍时间线偏移），跳过已见内容
                    start_page = max(1, int(last_page) - 1)

            collector = crawler.WeiboCollector(
                task.user_id, task.cookie,
                start_page=start_page, stop_on_seen_pages=stop_on_seen)
            if task.collected:
                collector.seen_ids = {
                    r[0] for r in store._conn.execute(
                        "SELECT bid FROM weibo WHERE user_id=?",
                        (task.user_id,)).fetchall()}

            def on_progress(ev):
                if ev["type"] == "user":
                    u = ev["user"]
                    task.total = u["statuses_count"]
                    task.screen_name = u["screen_name"]
                    task.action, task.action_until = "准备抓取微博", 0
                    self._log(task, f"用户 {u['screen_name']}，"
                                    f"微博总数 {u['statuses_count']}")
                elif ev["type"] == "page":
                    task.page = ev["page"]
                    task.collected += ev["got"]
                    task.action, task.action_until = f"抓取第 {ev['page']} 页", 0
                    if ev["got"]:
                        self._log(task, f"第 {ev['page']} 页，"
                                        f"累计 {task.collected} 条")
                elif ev["type"] == "rest":
                    task.action = "自动休息（防封会话上限）"
                    task.action_until = time.time() + ev["seconds"]
                    self._log(task, f"连续采集达到会话上限，"
                                    f"自动休息 {ev['seconds']} 秒后继续")

            summary = collector.crawl_all(store, on_progress=on_progress)
            csv_path = store.export_csv()
            new_count = summary["count"]
            total_in_db = store.count_weibos()
            if task.page:
                store.set_meta("last_page", str(task.page))
            store.set_meta("completed", "1")
            # 连续顺利跑满 10 分钟以上，说明当前 Cookie 状态良好，重置风控计数
            if time.time() - started_at >= 600:
                store.set_meta("ban_count", "0")
            task.state = "done"
            task.finished_at = datetime.now().strftime("%m-%d %H:%M:%S")
            task.action, task.action_until = "已完成", 0
            total = summary["user"]["statuses_count"]
            self._log(task, f"采集完成：本次新增 {new_count} 条，"
                            f"库中共 {total_in_db} 条，用户微博总数 {total}，"
                            f"CSV 已导出")
            if total and total_in_db < total * 0.95:
                self._log(task, f"提示：缺口 {total - total_in_db} 条，"
                                f"通常为仅自己可见或已删除的微博")
        except crawler.NeedCaptchaError as e:
            task.state = "need_captcha"
            task.captcha_url = e.url
            task.action, task.action_until = "等待人工验证", 0
            if store:
                if task.page:
                    store.set_meta("last_page", str(task.page))
                    store.set_meta("completed", "0")
                store.set_meta("ban_count",
                               str(int(store.get_meta("ban_count") or 0) + 1))
                store.set_meta("last_ban_at", str(int(time.time())))
            self._log(task, f"触发风控，采集在第 {task.page} 页附近暂停，"
                            f"需要人工验证：{e.url}")
        except Exception as e:
            task.state = "failed"
            task.error = f"{type(e).__name__}: {e}"
            task.finished_at = datetime.now().strftime("%m-%d %H:%M:%S")
            task.action, task.action_until = "采集失败", 0
            if store and task.page:
                store.set_meta("last_page", str(task.page))
                store.set_meta("completed", "0")
            task.logs.append(traceback.format_exc(limit=3))
            self._log(task, f"采集失败：{task.error}")
            log.error("task %s failed:\n%s", task.id, traceback.format_exc())
        finally:
            monitor_mod.manager.unpause(task.user_id)
            media_mod.manager.unpause(task.user_id)
            if store:
                store.close()


manager = TaskManager()
