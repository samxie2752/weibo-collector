#!/usr/bin/env python3
"""浏览器登录获取 Cookie：弹出有头 Chromium 打开微博登录页，用户手动登录
（扫码/账密均可），脚本轮询浏览器 Cookie，检测到 SUB 即捕获。

微博 wap 二维码接口返回的是加密私有格式（只有登录页 JS 能渲染），因此选择
让真实页面自己处理登录协议——对滑块/签名/格式变化免疫。

inject_cookie 参数仅为测试注入用（模拟已登录状态），正常使用不传。
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Dict, Optional

import requests

from .store import DATA_DIR
from .monitor import cookie_path

log = logging.getLogger("weibo.browserlogin")

LOGIN_URL = "https://m.weibo.cn/login/"
CONFIG_URL = "https://m.weibo.cn/api/config"
POLL_INTERVAL = 1.5
TIMEOUT = 300  # 秒，留足扫码时间


def build_cookie_header(cookies: list) -> str:
    """playwright cookies → 请求头字符串。同名 cookie 优先取 .weibo.cn 域。"""
    chosen: Dict[str, str] = {}
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        domain = c.get("domain", "")
        if "weibo.cn" in domain:
            chosen[name] = value  # wap 域优先，覆盖先写入的 weibo.com 版本
        elif name not in chosen:
            chosen[name] = value
    return "; ".join(f"{k}={v}" for k, v in chosen.items())


def check_login(cookie_header: str) -> tuple:
    """用 Cookie 调 api/config 判断登录态。返回 (uid, 是否已确认)。

    (uid, True) = 确认登录；(非空 uid 必然 definite)
    ("", True)  = 确认未登录（访客 SUB 等）——可以记住并跳过
    ("", False) = 网络异常没确认——下轮应重试
    """
    try:
        r = requests.get(CONFIG_URL, timeout=10, headers={
            "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like "
                           "Mac OS X) AppleWebKit/605.1.15 (KHTML, like "
                           "Gecko) Version/17.4 Mobile/15E148 Safari/604.1"),
            "Cookie": cookie_header,
            "Referer": "https://m.weibo.cn/",
        })
        d = (r.json().get("data") or {})
        if d.get("login"):
            return str(d.get("uid") or ""), True
        return "", True
    except Exception as e:
        log.warning("api/config check failed: %s", e)
        return "", False


class BrowserLoginSession:
    """一次浏览器登录会话。状态：launching → waiting → success/failed。

    登录的是用户自己的账号（uid 可能与采集目标不同）：
    Cookie 存到登录账号名下；若指定了 target_uid（采集目标）且该账号
    已有数据目录，同时复制一份，让"继续采集/监控"能免粘贴复用。
    """

    def __init__(self, sid: str, target_uid: str = "", inject_cookie: str = ""):
        self.sid = sid
        self.target_uid = target_uid
        self.inject_cookie = inject_cookie
        self.status = "launching"   # launching / waiting / success / failed
        self.error = ""
        self.note = ""              # 等待期间的补充提示（如：检测到访客身份）
        self.uid = ""               # 登录账号 uid
        self.cookie = ""
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"browserlogin-{sid}")
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def info(self) -> Dict:
        return {"sid": self.sid, "status": self.status, "error": self.error,
                "note": self.note, "uid": self.uid, "cookie": self.cookie}

    def _set(self, status: str, error: str = ""):
        self.status = status
        self.error = error

    def _save_cookie(self):
        """Cookie 存到登录账号名下；采集目标账号（若有目录）同步一份。"""
        import os
        os.makedirs(os.path.join(DATA_DIR, self.uid), exist_ok=True)
        with open(cookie_path(self.uid), "w", encoding="utf-8") as f:
            f.write(self.cookie)
        if (self.target_uid and self.target_uid != self.uid
                and self.target_uid.isdigit()):
            target_dir = os.path.join(DATA_DIR, self.target_uid)
            if os.path.isdir(target_dir):
                with open(cookie_path(self.target_uid), "w",
                          encoding="utf-8") as f:
                    f.write(self.cookie)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._set("failed", "缺少 playwright：.venv/bin/pip install playwright && "
                                ".venv/bin/playwright install chromium")
            return
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False, args=["--window-size=420,760"])
                context = browser.new_context(
                    viewport={"width": 400, "height": 720},
                    user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 "
                                "like Mac OS X) AppleWebKit/605.1.15 "
                                "(KHTML, like Gecko) Version/17.4 "
                                "Mobile/15E148 Safari/604.1"))
                if self.inject_cookie:
                    # 测试钩子：模拟已登录
                    name, _, value = self.inject_cookie.partition("=")
                    context.add_cookies([{
                        "name": name, "value": value,
                        "domain": ".weibo.cn", "path": "/"}])
                page = context.new_page()
                page.goto(LOGIN_URL, timeout=30000)
                self._set("waiting")
                deadline = time.time() + TIMEOUT
                last_sub = ""       # 已验证过且确认非登录态的 SUB 值
                while time.time() < deadline:
                    if self._cancel.is_set():
                        self._set("failed", "已取消")
                        break
                    cookies = context.cookies()
                    subs = [c["value"] for c in cookies if c["name"] == "SUB"]
                    sub = subs[-1] if subs else ""
                    if sub:
                        header = build_cookie_header(cookies)
                        if sub != last_sub:
                            # SUB 出现或变化才验证：访客 SUB 会被确认拒绝并跳过，
                            # 真正登录后 SUB 会变成新值再次进入验证
                            uid, definite = check_login(header)
                            if uid:
                                self.cookie = header
                                self.uid = uid
                                self._save_cookie()
                                self._set("success")
                                break
                            if definite:
                                last_sub = sub
                                self.note = ("检测到访客身份（非登录），"
                                             "请在窗口内完成登录")
                            # 网络异常（definite=False）不记录，下轮重试
                    if not browser.is_connected():
                        self._set("failed", "浏览器窗口被关闭，未完成登录")
                        break
                    time.sleep(POLL_INTERVAL)
                else:
                    self._set("failed", "超时未检测到登录（5 分钟）")
                if self.status == "success":
                    log.info("browser login captured uid=%s", self.uid)
                try:
                    browser.close()
                except Exception:
                    pass
        except Exception as e:
            self._set("failed", f"{type(e).__name__}: {e}")
            log.exception("browser login failed")


class BrowserLoginManager:
    def __init__(self):
        self.sessions: Dict[str, BrowserLoginSession] = {}
        self._lock = threading.Lock()

    def create(self, target_uid: str = "", inject_cookie: str = "") -> BrowserLoginSession:
        sid = uuid.uuid4().hex[:12]
        s = BrowserLoginSession(sid, target_uid=target_uid,
                                inject_cookie=inject_cookie)
        with self._lock:
            # 只保留最近 5 个会话
            for old in list(self.sessions)[:-4]:
                self.sessions.pop(old, None)
            self.sessions[sid] = s
        return s

    def get(self, sid: str) -> Optional[BrowserLoginSession]:
        return self.sessions.get(sid)


manager = BrowserLoginManager()
