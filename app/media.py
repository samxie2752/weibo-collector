#!/usr/bin/env python3
"""图片下载器：把 weibo 表里已采集的图片 URL 落盘到 data/{uid}/media/。

设计要点：
  - 与文本采集解耦：独立线程/独立任务，采集任务运行期间自动暂停
  - 文件名 = {bid}_{序号}.{扩展名}，存在即跳过 → 天然去重与断点续传
  - 只处理图片（微博视频 CDN 风控更敏感且 URL 带时效，交给 yt-dlp 类工具更合适）
  - 下载走 sinaimg CDN，比接口宽容得多，但仍保留短随机延迟
"""
from __future__ import annotations

import glob
import logging
import os
import random
import threading
import time
from typing import Dict, List, Optional, Tuple

import requests

from .store import DATA_DIR, Store

log = logging.getLogger("weibo.media")

IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp"}
DELAY = (0.4, 1.2)          # 图片之间的随机延迟（秒）
MIN_SIZE = 1024             # 小于 1KB 视为反盗链占位图，判失败
RETRY = 2

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 "
                   "Safari/537.36"),
    "Referer": "https://weibo.com/",
}


def media_dir(uid: str) -> str:
    return os.path.join(DATA_DIR, str(uid), "media")


def ext_from_url(url: str) -> str:
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext if ext in IMAGE_EXT else "jpg"


def media_filename(bid: str, idx: int, url: str) -> str:
    return f"{bid}_{idx}.{ext_from_url(url)}"


def collect_jobs(uid: str, store: Store) -> List[Tuple[str, int, str, str]]:
    """扫描缺文件的图片，返回 [(bid, idx, url, 本地路径), ...]。"""
    jobs: List[Tuple[str, int, str, str]] = []
    rows = store._conn.execute(
        "SELECT bid, pic_urls FROM weibo WHERE user_id=? AND pic_urls != ''",
        (uid,)).fetchall()
    d = media_dir(uid)
    for bid, urls in rows:
        for idx, url in enumerate(urls.split(","), 1):
            url = url.strip()
            if not url:
                continue
            path = os.path.join(d, media_filename(bid, idx, url))
            if not os.path.exists(path):
                jobs.append((bid, idx, url, path))
    return jobs


def download_one(session: requests.Session, url: str, path: str) -> bool:
    """下载单张图，成功落盘返回 True。失败重试 RETRY 次。"""
    for attempt in range(RETRY):
        try:
            r = session.get(url, timeout=15, headers=HEADERS)
            if (r.status_code == 200
                    and len(r.content) > MIN_SIZE
                    and r.headers.get("Content-Type", "image/").startswith("image")):
                tmp = path + ".part"
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, path)  # 原子落盘，避免半截文件被当成已下载
                return True
        except requests.RequestException as e:
            log.debug("download %s failed: %s", url, e)
        time.sleep(2 * (attempt + 1))
    return False


class MediaState:
    def __init__(self, uid: str):
        self.uid = uid
        self.state = "running"     # running / done / partial / failed / cancelled
        self.downloaded = 0
        self.total = 0
        self.errors = 0
        self.last_error = ""
        self._cancel = threading.Event()
        self._paused = False

    def info(self) -> Dict:
        return {
            "uid": self.uid, "state": self.state,
            "downloaded": self.downloaded, "total": self.total,
            "errors": self.errors, "last_error": self.last_error,
        }


class MediaManager:
    def __init__(self):
        self.states: Dict[str, MediaState] = {}
        self._lock = threading.Lock()

    def start(self, uid: str) -> MediaState:
        with self._lock:
            st = self.states.get(uid)
            if st and st.state == "running":
                return st  # 已在跑
            st = MediaState(uid)
            self.states[uid] = st
        threading.Thread(target=self._loop, args=(st,), daemon=True,
                         name=f"media-{uid}").start()
        return st

    def stop(self, uid: str):
        st = self.states.get(uid)
        if st:
            st._cancel.set()

    def pause(self, uid: str):
        st = self.states.get(uid)
        if st:
            st._paused = True

    def unpause(self, uid: str):
        st = self.states.get(uid)
        if st:
            st._paused = False

    def info(self, uid: str) -> Optional[Dict]:
        st = self.states.get(uid)
        return st.info() if st else None

    # ---------- 工作线程 ----------

    def _loop(self, st: MediaState):
        try:
            store = Store(st.uid)
            jobs = collect_jobs(st.uid, store)
            store.close()
            st.total = len(jobs)
            if not jobs:
                st.state = "done"
                return
            os.makedirs(media_dir(st.uid), exist_ok=True)
            log.info("media %s: %d images to download", st.uid, len(jobs))
            session = requests.Session()
            for bid, idx, url, path in jobs:
                while st._paused and not st._cancel.is_set():
                    time.sleep(2)
                if st._cancel.is_set():
                    st.state = "cancelled"
                    return
                if download_one(session, url, path):
                    st.downloaded += 1
                else:
                    st.errors += 1
                    st.last_error = url[:120]
                if st.downloaded and st.downloaded % 20 == 0:
                    log.info("media %s: %d/%d", st.uid, st.downloaded, st.total)
                time.sleep(random.uniform(*DELAY))
            st.state = "done" if st.errors == 0 else "partial"
            log.info("media %s finished: %d ok, %d failed",
                     st.uid, st.downloaded, st.errors)
        except Exception as e:
            st.state = "failed"
            st.last_error = f"{type(e).__name__}: {e}"
            log.exception("media download failed")


manager = MediaManager()


def find_media_file(uid: str, bid: str, idx: int) -> Optional[str]:
    """按 {bid}_{idx}.* 定位已下载的文件（供 HTTP 服务用）。"""
    if not bid.isalnum() or idx < 1:
        return None
    matches = glob.glob(os.path.join(media_dir(uid), f"{bid}_{idx}.*"))
    return matches[0] if matches else None
