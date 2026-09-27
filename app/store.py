#!/usr/bin/env python3
"""SQLite 存储：每账号一个库 data/{uid}/weibo.db，支持分页查询和 CSV 导出。"""
from __future__ import annotations

import csv
import io
import os
import sqlite3
import threading
from typing import Dict, List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user (
    id TEXT PRIMARY KEY,
    screen_name TEXT, gender TEXT, description TEXT,
    statuses_count INTEGER, followers_count INTEGER, follow_count INTEGER,
    verified INTEGER, verified_reason TEXT,
    profile_image_url TEXT, avatar_hd TEXT,
    registration_time TEXT, birthday TEXT, location TEXT,
    ip_location TEXT, sunshine TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS weibo (
    bid TEXT PRIMARY KEY,
    user_id TEXT,
    created_at TEXT,
    text TEXT,
    source TEXT,
    reposts_count INTEGER, comments_count INTEGER, attitudes_count INTEGER,
    is_retweet INTEGER,
    retweet_bid TEXT, retweet_user TEXT, retweet_text TEXT, retweet_created_at TEXT,
    pic_urls TEXT, video_url TEXT, article_url TEXT,
    location TEXT, is_long INTEGER, edited INTEGER, is_pinned INTEGER,
    crawled_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_weibo_created ON weibo(user_id, created_at);
"""

_WEIBO_COLUMNS = ["bid", "user_id", "created_at", "text", "source",
                  "reposts_count", "comments_count", "attitudes_count",
                  "is_retweet", "retweet_bid", "retweet_user", "retweet_text",
                  "retweet_created_at", "pic_urls", "video_url", "article_url",
                  "location", "is_long", "edited", "is_pinned", "crawled_at"]


def db_path(user_id: str) -> str:
    return os.path.join(DATA_DIR, str(user_id), "weibo.db")


def list_account_ids() -> list:
    """扫描 data/ 目录，返回所有有 weibo.db 的账号 uid。"""
    out = []
    if not os.path.isdir(DATA_DIR):
        return out
    for name in os.listdir(DATA_DIR):
        if os.path.exists(os.path.join(DATA_DIR, name, "weibo.db")):
            out.append(name)
    return sorted(out)


class Store:
    """线程安全封装（同一任务内单线程写，查询可能来自 API 线程）。"""

    def __init__(self, user_id: str):
        self.user_id = str(user_id)
        os.makedirs(os.path.dirname(db_path(user_id)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path(user_id), check_same_thread=False,
                                     timeout=15)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------- 写入 ----------

    def save_user(self, user: Dict):
        cols = ",".join(user.keys())
        ph = ",".join("?" * len(user))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO user ({cols}) VALUES ({ph})",
                list(user.values()))
            self._conn.commit()

    def save_weibo(self, wb: Dict):
        from datetime import datetime
        wb = dict(wb)
        wb.setdefault("crawled_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        cols = [c for c in _WEIBO_COLUMNS if c in wb]
        ph = ",".join("?" * len(cols))
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO weibo ({','.join(cols)}) VALUES ({ph})",
                [wb[c] for c in cols])
            self._conn.commit()

    # ---------- 采集进度元数据 ----------

    def set_meta(self, key: str, value: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value))
            self._conn.commit()

    def get_meta(self, key: str) -> str:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?",
                                 (key,)).fetchone()
        return row[0] if row else ""

    # ---------- 查询 ----------

    def get_user(self) -> Optional[Dict]:
        row = self._conn.execute("SELECT * FROM user WHERE id=?",
                                 (self.user_id,)).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self._conn.execute("SELECT * FROM user LIMIT 0").description]
        return dict(zip(cols, row))

    def count_weibos(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM weibo WHERE user_id=?",
            (self.user_id,)).fetchone()[0]

    def last_weibo_at(self) -> str:
        row = self._conn.execute(
            "SELECT MAX(created_at) FROM weibo WHERE user_id=?",
            (self.user_id,)).fetchone()
        return row[0] or ""

    def list_weibos(self, page: int = 1, size: int = 20) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM weibo WHERE user_id=? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (self.user_id, size, (page - 1) * size)).fetchall()
        return [dict(zip(_WEIBO_COLUMNS, r)) for r in rows]

    # ---------- 导出 ----------

    def export_csv(self) -> str:
        """导出 CSV 到 data/{uid}/{uid}.csv，返回文件路径。"""
        out = os.path.join(DATA_DIR, self.user_id, f"{self.user_id}.csv")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM weibo WHERE user_id=? ORDER BY created_at DESC",
                (self.user_id,)).fetchall()
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(_WEIBO_COLUMNS)
            writer.writerows(rows)
        return out
