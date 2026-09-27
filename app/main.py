#!/usr/bin/env python3
"""FastAPI 入口：页面 + 采集/账号/监控 API。仅绑定本机。"""
from __future__ import annotations

import os
import shutil

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import browserlogin as browserlogin_mod
from . import media as media_mod
from . import monitor as monitor_mod
from . import tasks
from .store import DATA_DIR, Store, list_account_ids

app = FastAPI(title="微博历史采集", docs_url=None, redoc_url=None)


class CollectReq(BaseModel):
    user_id: str = Field(min_length=1, description="微博数字 UID")
    cookie: str = Field(default="", description="浏览器复制的微博 Cookie")


class ResumeReq(BaseModel):
    cookie: str = Field(default="", description="可选：换新的 Cookie")


class MonitorReq(BaseModel):
    interval_seconds: int = Field(default=300, description="检查间隔（秒），下限 60")
    cookie: str = Field(default="", description="可选：不填则复用上次保存的 Cookie")


class ContinueReq(BaseModel):
    cookie: str = Field(default="", description="可选：不填则复用上次保存的 Cookie")


class BrowserLoginReq(BaseModel):
    target_uid: str = Field(default="",
                            description="可选：采集目标 uid，成功后 Cookie 同时复制给该账号")


@app.on_event("startup")
def _startup():
    monitor_mod.manager.auto_resume()


@app.get("/")
def index():
    return FileResponse(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "static", "index.html"))


# ---------- 采集任务 ----------

@app.post("/api/collect")
def collect(req: CollectReq):
    uid = req.user_id.strip()
    if not uid.isdigit():
        raise HTTPException(400, "user_id 必须是数字 UID（微博主页 m.weibo.cn/u/数字 里的数字）")
    task = tasks.manager.create(uid, req.cookie)
    return {"task_id": task.id}


@app.get("/api/tasks")
def list_tasks():
    return tasks.manager.list()


@app.post("/api/tasks/{task_id}/resume")
def resume_task(task_id: str, req: ResumeReq):
    task = tasks.manager.resume(task_id, req.cookie)
    if not task:
        raise HTTPException(404, "任务不存在或当前状态不可继续")
    return {"ok": True}


# ---------- 已采集账号 ----------

@app.get("/api/accounts")
def accounts():
    out = []
    for uid in list_account_ids():
        store = Store(uid)
        try:
            user = store.get_user() or {}
            out.append({
                "uid": uid,
                "screen_name": user.get("screen_name", ""),
                "avatar": (user.get("avatar_hd")
                           or user.get("profile_image_url") or ""),
                "count": store.count_weibos(),
                "statuses_count": user.get("statuses_count", 0),
                "last_weibo_at": store.last_weibo_at(),
                "monitor": monitor_mod.manager.info(uid),
                "media": media_mod.manager.info(uid),
                # 断点续采信息（风控计数为时间衰减后的有效值）
                "resume": {
                    "last_page": store.get_meta("last_page"),
                    "completed": store.get_meta("completed"),
                    "ban_count": tasks.effective_ban_count(
                        int(store.get_meta("ban_count") or 0),
                        store.get_meta("last_ban_at")),
                    "cookie_saved": os.path.exists(
                        monitor_mod.cookie_path(uid)),
                },
                "task_running": tasks.manager.running_task_for(uid) is not None,
            })
        finally:
            store.close()
    out.sort(key=lambda a: a["count"], reverse=True)
    return out


@app.get("/api/accounts/{uid}/weibos")
def account_weibos(uid: str, page: int = 1, size: int = 20):
    if uid not in list_account_ids():
        raise HTTPException(404, "账号不存在")
    store = Store(uid)
    try:
        return {
            "user": store.get_user(),
            "total": store.count_weibos(),
            "page": page,
            "size": size,
            "weibos": store.list_weibos(page, size),
        }
    finally:
        store.close()


@app.get("/api/accounts/{uid}/export")
def account_export(uid: str):
    if uid not in list_account_ids():
        raise HTTPException(404, "账号不存在")
    store = Store(uid)
    try:
        path = store.export_csv()
    finally:
        store.close()
    return FileResponse(path, filename=f"weibo_{uid}.csv",
                        media_type="text/csv")


@app.get("/api/accounts/{uid}/export.md")
def account_export_markdown(uid: str):
    """按月分节的 Markdown 时间线（图片优先引用本地文件）。"""
    if uid not in list_account_ids():
        raise HTTPException(404, "账号不存在")
    store = Store(uid)
    try:
        path = store.export_markdown()
    finally:
        store.close()
    return FileResponse(path, filename=f"weibo_{uid}.md",
                        media_type="text/markdown")


@app.delete("/api/accounts/{uid}")
def account_delete(uid: str):
    monitor_mod.manager.stop(uid)
    path = os.path.join(DATA_DIR, uid)
    if not os.path.isdir(path):
        raise HTTPException(404, "账号不存在")
    shutil.rmtree(path)
    return {"ok": True}


@app.post("/api/accounts/{uid}/continue")
def account_continue(uid: str, req: ContinueReq):
    """一键断点续采：带历史校验（防重复任务、Cookie 可用性），复用断点元数据。"""
    if uid not in list_account_ids():
        raise HTTPException(404, "账号不存在")
    running = tasks.manager.running_task_for(uid)
    if running:
        phase = "排队中" if running.state == "queued" else "采集中"
        raise HTTPException(409, f"该账号已有任务在{phase}"
                                 f"（任务 {running.id}），请等它结束或看任务列表")
    cookie = req.cookie.strip()
    if not cookie:
        cp = monitor_mod.cookie_path(uid)
        if os.path.exists(cp):
            with open(cp, encoding="utf-8") as f:
                cookie = f.read().strip()
    if not cookie:
        raise HTTPException(400, "没有可用的 Cookie：请先在上方 Cookie 框粘贴后重试")
    task = tasks.manager.create(uid, cookie)
    return {"task_id": task.id}


# ---------- 监控 ----------

@app.post("/api/accounts/{uid}/monitor")
def monitor_start(uid: str, req: MonitorReq):
    if uid not in list_account_ids():
        raise HTTPException(404, "账号不存在")
    try:
        st = monitor_mod.manager.start(uid, req.interval_seconds, req.cookie)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return st.info()


@app.delete("/api/accounts/{uid}/monitor")
def monitor_stop(uid: str):
    monitor_mod.manager.stop(uid)
    return {"ok": True}


# ---------- 浏览器登录自动获取 Cookie ----------

@app.post("/api/cookie/browser")
def cookie_browser(req: BrowserLoginReq):
    """弹出有头浏览器打开微博登录页，用户手动登录后自动捕获 Cookie。"""
    s = browserlogin_mod.manager.create(target_uid=req.target_uid.strip())
    return s.info()


@app.get("/api/cookie/browser/{sid}")
def cookie_browser_status(sid: str):
    s = browserlogin_mod.manager.get(sid)
    if not s:
        raise HTTPException(404, "会话不存在或已过期")
    return s.info()


@app.delete("/api/cookie/browser/{sid}")
def cookie_browser_cancel(sid: str):
    s = browserlogin_mod.manager.get(sid)
    if not s:
        raise HTTPException(404, "会话不存在或已过期")
    s.cancel()
    return {"ok": True}


# ---------- 图片下载 ----------

CONTENT_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                 "gif": "image/gif", "webp": "image/webp"}


@app.post("/api/accounts/{uid}/media")
def media_start(uid: str):
    if uid not in list_account_ids():
        raise HTTPException(404, "账号不存在")
    running = tasks.manager.running_task_for(uid)
    if running:
        raise HTTPException(409, "该账号有采集任务进行中，结束后再下载图片")
    return media_mod.manager.start(uid).info()


@app.get("/api/accounts/{uid}/media")
def media_status(uid: str):
    return media_mod.manager.info(uid) or {"uid": uid, "state": "idle",
                                           "downloaded": 0, "total": 0,
                                           "errors": 0, "last_error": ""}


@app.delete("/api/accounts/{uid}/media")
def media_stop(uid: str):
    media_mod.manager.stop(uid)
    return {"ok": True}


@app.get("/api/accounts/{uid}/media/{bid}/{idx}")
def media_file(uid: str, bid: str, idx: int):
    path = media_mod.find_media_file(uid, bid, idx)
    if not path:
        raise HTTPException(404, "图片未下载")
    ext = path.rsplit(".", 1)[-1].lower()
    return FileResponse(path, media_type=CONTENT_TYPES.get(ext, "image/jpeg"))
