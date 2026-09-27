#!/usr/bin/env python3
"""自研微博移动端采集核心。

仅依赖 requests + 标准库。接口行为参考 dataabc/weibo-crawler（见 reference/），
代码独立实现，针对 Web 服务场景做了改造：
  - 不使用 sys.exit / 终端交互，验证码、登录墙以异常形式抛给上层任务管理器
  - 每个请求间随机延迟 + 批次休息，降低风控触发概率
  - 长微博 detail 页从 HTML 抠内嵌 JSON（与参考实现同思路）
"""
from __future__ import annotations

import html as html_mod
import json
import random
import re
import threading
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Callable, Dict, Generator, List, Optional

import requests

API = "https://m.weibo.cn/api/container/getIndex"
DETAIL = "https://m.weibo.cn/detail/{id}"

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

# 指纹轮换池（对齐参考实现的 anti_ban_config）
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.113 Mobile Safari/537.36",
]
ACCEPT_LANGS = [
    "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "zh-TW,zh;q=0.9,en;q=0.8",
]
REFERERS = ["https://m.weibo.cn/", "https://weibo.com/", "https://www.weibo.com/"]

PAGE_DELAY = (8.0, 15.0)       # 每页请求之间的随机延迟（秒），与参考实现一致
PAGES_PER_REST = 5             # 每采多少页额外休息一次
REST_DELAY = (30.0, 60.0)      # 批次休息时长（秒）
MAX_RETRIES = 5                # 单请求最大重试次数
PAGE_COUNT = 20                # 每页条数（微博允许 10~50）

GLOBAL_MIN_GAP = 3.0           # 所有采集器共享：任意两次微博请求的最小间隔（秒）
SESSION_WEIBO_LIMIT = 300      # 单轮连续采集条数上限，超过自动长休息
SESSION_TIME_LIMIT = 480       # 单轮连续采集时长上限（秒）
SESSION_REST = (180.0, 300.0)  # 达到上限后的长休息时长（秒）
DETAIL_DELAY = (2.0, 4.0)      # 长微博 detail 页请求前延迟


class CrawlError(Exception):
    """一般采集失败（重试耗尽、接口异常等）。"""


class NeedCaptchaError(CrawlError):
    """触发风控：需要用户在浏览器完成验证（url 为验证/登录页）。"""

    def __init__(self, url: str, message: str = ""):
        self.url = url
        super().__init__(message or f"需要验证/登录: {url}")


class _TextExtractor(HTMLParser):
    """把微博正文 HTML 片段转成纯文本，<br> 转换行。"""

    def __init__(self):
        super().__init__()
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    p = _TextExtractor()
    try:
        p.feed(fragment)
        p.close()
        text = "".join(p.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", "", fragment)
    return html_mod.unescape(text).replace("\xa0", " ").strip()


def string_to_int(value) -> int:
    """'1.2万' / '3亿+' / 123 → 整数。"""
    if isinstance(value, int):
        return value
    s = str(value).replace("+", "").replace(" ", "")
    try:
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100000000)
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        return int(float(s))
    except ValueError:
        return 0


def standardize_date(created_at: str) -> str:
    """把列表接口的相对/美式日期统一为 'YYYY-MM-DD HH:MM:SS'。"""
    now = datetime.now()
    if "刚刚" in created_at:
        ts = now
    elif "分钟" in created_at:
        ts = now - timedelta(minutes=int(created_at[: created_at.find("分钟")]))
    elif "小时" in created_at:
        ts = now - timedelta(hours=int(created_at[: created_at.find("小时")]))
    elif "昨天" in created_at:
        base = now - timedelta(days=1)
        m = re.search(r"(\d{1,2}):(\d{2})", created_at)
        ts = (base.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                           second=0, microsecond=0) if m else base)
    else:
        ts = datetime.strptime(created_at.replace("+0800 ", ""), "%c")
    return ts.strftime("%Y-%m-%d %H:%M:%S")


class WeiboCollector:
    """单账号采集器。用法：

        c = WeiboCollector(uid, cookie)
        user = c.fetch_user_info()
        for progress in c.iter_weibos():
            ...
    """

    _gap_lock = threading.Lock()
    _last_req = 0.0

    @classmethod
    def _throttle(cls):
        """全局节流：任意实例的两次微博请求之间至少间隔 GLOBAL_MIN_GAP 秒
        （任务线程 + 监控线程共用一把闸）。"""
        with cls._gap_lock:
            wait = GLOBAL_MIN_GAP - (time.time() - cls._last_req)
            if wait > 0:
                time.sleep(wait)
            cls._last_req = time.time()

    def _random_headers(self) -> Dict:
        h = dict(BASE_HEADERS)
        h["User-Agent"] = random.choice(USER_AGENTS)
        h["Accept-Language"] = random.choice(ACCEPT_LANGS)
        h["Referer"] = random.choice(REFERERS)
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def __init__(self, user_id: str, cookie: str = "",
                 since_date: str = "2009-08-01",
                 start_page: int = 1,
                 stop_on_seen_pages: int = 0):
        self.user_id = str(user_id).strip()
        self.cookie = cookie.strip()
        self.since_date = since_date
        # 断点续采：从上次中断的页继续（0 则从第 1 页）
        self.start_page = max(1, int(start_page))
        # >0 时：连续 N 页都没有新内容才认为到尽头（用于增量模式）；
        # 0 时：仅靠空页/起始日期/页数上限结束（用于续采模式，允许跳过已见页）
        self.stop_on_seen_pages = int(stop_on_seen_pages)
        self.expected_total = 0  # 用户微博总数，用于翻页安全上限
        self.session = self._build_session()
        # 断点恢复：跳过已采集的 bid（置顶微博会反复出现在第一页）
        self.seen_ids: set = set()

    # ---------- 会话 ----------

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        # 预热：拿 MLOGIN/_T_WM/XSRF-TOKEN 等指纹 cookie
        try:
            self._throttle()
            s.get("https://m.weibo.cn/", timeout=10,
                  headers=self._random_headers())
        except requests.RequestException:
            pass  # 预热失败不致命，带完整 cookie 时不需要它
        return s

    # ---------- 底层请求 ----------

    def _get_json(self, params: Dict) -> Dict:
        last_err: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._throttle()
                r = self.session.get(API, params=params, timeout=10,
                                     headers=self._random_headers())
                r.raise_for_status()
                js = r.json()
                ok = js.get("ok")
                if ok == 1:
                    return js
                url = js.get("url") or ""
                if isinstance(url, str) and url.strip():
                    # -100: 登录墙；其余: 验证码挑战——都需要人工在浏览器处理
                    raise NeedCaptchaError(url.strip())
                # ok=0：数据为空或临时异常，退避后重试
                last_err = CrawlError(f"接口返回异常: {json.dumps(js, ensure_ascii=False)[:200]}")
            except (requests.RequestException, ValueError) as e:
                last_err = e
            time.sleep(min(5 * 2 ** attempt, 60))
        raise CrawlError(f"重试 {MAX_RETRIES} 次仍失败: {last_err}")

    # ---------- 用户资料 ----------

    def fetch_user_info(self) -> Dict:
        js = self._get_json({"containerid": "100505" + self.user_id})
        info = js["data"]["userInfo"]
        user = {
            "id": self.user_id,
            "screen_name": info.get("screen_name", ""),
            "gender": {"m": "男", "f": "女"}.get(info.get("gender"), ""),
            "description": info.get("description", ""),
            "statuses_count": string_to_int(info.get("statuses_count", 0)),
            "followers_count": string_to_int(info.get("followers_count", 0)),
            "follow_count": string_to_int(info.get("follow_count", 0)),
            "verified": bool(info.get("verified")),
            "verified_reason": info.get("verified_reason", ""),
            "profile_image_url": info.get("profile_image_url", ""),
            "avatar_hd": info.get("avatar_hd", ""),
            "registration_time": "",
            "birthday": "",
            "location": "",
            "ip_location": "",
            "sunshine": "",
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        # 详细资料卡片（生日/所在地/注册时间等，失败不影响主流程）
        try:
            js2 = self._get_json(
                {"containerid": f"230283{self.user_id}_-_INFO"})
            cards = js2.get("data", {}).get("cards", [])
            groups = []
            for c in cards[:2]:
                groups += c.get("card_group", [])
            mapping = {"生日": "birthday", "所在地": "location",
                       "IP属地": "ip_location", "注册时间": "registration_time",
                       "阳光信用": "sunshine"}
            for g in groups:
                key = mapping.get(g.get("item_name"))
                if key:
                    user[key] = g.get("item_content", "")
        except CrawlError:
            pass
        return user

    # ---------- 长微博 ----------

    def _fetch_long_text(self, weibo_id: str) -> Optional[Dict]:
        """detail 页内嵌 JSON（含全文），失败返回 None 由调用方降级。"""
        for i in range(3):
            try:
                time.sleep(random.uniform(*DETAIL_DELAY))
                self._throttle()
                r = self.session.get(DETAIL.format(id=weibo_id), timeout=10,
                                     headers=self._random_headers())
                html = r.text
                frag = html[html.find('"status":'):]
                frag = frag[:frag.rfind('"call"')]
                frag = frag[:frag.rfind(",")]
                js = json.loads("{" + frag + "}", strict=False)
                status = js.get("status")
                if status:
                    return status
            except (requests.RequestException, ValueError):
                if i == 2:
                    return None
        return None

    # ---------- 单条解析 ----------

    def _parse_weibo(self, mb: Dict) -> Dict:
        page_info = mb.get("page_info") or {}
        video_url = ""
        if page_info.get("type") == "video":
            media = page_info.get("media_info") or {}
            video_url = (media.get("playback_url")
                         or media.get("mp4_hd_url")
                         or media.get("mp4_720p_mp4")
                         or "")
        article_url = ""
        if page_info.get("type") == "article":
            article_url = page_info.get("page_url", "")

        pics = []
        for p in mb.get("pics") or []:
            if isinstance(p, dict) and p.get("type") != "video":
                url = (p.get("large") or {}).get("url") or p.get("url") or ""
                if url:
                    pics.append(url)

        rt = mb.get("retweeted_status")
        is_retweet = bool(rt and rt.get("id"))

        return {
            "bid": str(mb.get("id", "")),
            "user_id": self.user_id,
            "created_at": mb.get("created_at", ""),   # 原始格式，入库前标准化
            "text": html_to_text(mb.get("text", "")),
            "source": html_to_text(mb.get("source", "")),
            "reposts_count": string_to_int(mb.get("reposts_count", 0)),
            "comments_count": string_to_int(mb.get("comments_count", 0)),
            "attitudes_count": string_to_int(mb.get("attitudes_count", 0)),
            "is_retweet": int(is_retweet),
            "retweet_bid": str(rt.get("id", "")) if is_retweet else "",
            "retweet_user": (rt.get("user") or {}).get("screen_name", "") if is_retweet else "",
            "retweet_text": html_to_text(rt.get("text", "")) if is_retweet else "",
            "retweet_created_at": rt.get("created_at", "") if is_retweet else "",
            "pic_urls": ",".join(pics),
            "video_url": video_url,
            "article_url": article_url,
            "location": mb.get("status_city", "") or "",
            "is_long": int(bool(mb.get("isLongText")) or (mb.get("pic_num") or 0) > 9),
            "edited": int((mb.get("edit_count") or 0) > 0),
            "is_pinned": int((mb.get("mblogtype") or 0) == 2
                             or (mb.get("title") or {}).get("text") == "置顶"),
        }

    # ---------- 主循环 ----------

    def _max_pages(self) -> int:
        """安全上限：按微博总数估算的页数 + 余量。"""
        if self.expected_total:
            return min(self.expected_total // PAGE_COUNT + 5, 10000)
        return 10000

    def iter_weibos(self, on_progress: Optional[Callable[[Dict], None]] = None
                    ) -> Generator[Dict, None, None]:
        """逐条产出标准化后的微博，直到采完历史/到达 since_date。

        on_progress 收到: {"type":"page","page":N,"got":X,"total":Y}
        """
        def notify(ev):
            if on_progress:
                try:
                    on_progress(ev)
                except Exception:
                    pass

        total = 0
        page = self.start_page
        stop = False
        seen_run = 0  # 连续"整页无新内容"的页数
        session_weibos = 0
        session_start = time.time()
        since = datetime.strptime(self.since_date, "%Y-%m-%d")
        while not stop:
            # 会话上限：连续采太久/太多会显著提高风控概率，主动长休息
            if (session_weibos >= SESSION_WEIBO_LIMIT
                    or time.time() - session_start >= SESSION_TIME_LIMIT):
                rest = random.uniform(*SESSION_REST)
                notify({"type": "rest", "seconds": round(rest),
                        "reason": "session_limit"})
                time.sleep(rest)
                session_weibos = 0
                session_start = time.time()
            if page > self._max_pages():
                break
            js = self._get_json({"containerid": "230413" + self.user_id,
                                 "page": page, "count": PAGE_COUNT})
            cards = js.get("data", {}).get("cards", [])
            if not cards:
                break
            got = 0
            for card in cards:
                if card.get("card_type") == 11:
                    group = card.get("card_group") or [card]
                    card = group[0] if group else card
                if card.get("card_type") != 9:
                    continue
                mb = card.get("mblog") or {}
                bid = str(mb.get("id", ""))
                if not bid or bid in self.seen_ids:
                    continue
                wb = self._parse_weibo(mb)
                # 长微博取全文（转发源不取，省请求）
                if wb["is_long"]:
                    full = self._fetch_long_text(wb["bid"])
                    if full:
                        wb["text"] = html_to_text(full.get("text", ""))
                if wb["created_at"]:
                    wb["created_at"] = standardize_date(wb["created_at"])
                    # 转发的源微博时间也一并标准化
                    if wb["retweet_created_at"]:
                        try:
                            wb["retweet_created_at"] = standardize_date(
                                wb["retweet_created_at"])
                        except ValueError:
                            wb["retweet_created_at"] = ""
                # 结束条件：到达起始日期（置顶微博不受影响，继续向后翻页）
                try:
                    dt = datetime.strptime(wb["created_at"], "%Y-%m-%d %H:%M:%S")
                    older_than_since = dt < since
                except ValueError:
                    older_than_since = False
                if older_than_since and not wb["is_pinned"]:
                    stop = True
                    break
                self.seen_ids.add(bid)
                got += 1
                total += 1
                session_weibos += 1
                yield wb
                if wb["is_pinned"]:
                    # 置顶微博不计入"本页采到条数"，避免干扰进度
                    got -= 1
                    total -= 1
            if got == 0:
                seen_run += 1
                if seen_run >= 10:
                    # 连续 10 页（约200条）全是已见内容：库已追平，防死循环
                    break
                if (self.stop_on_seen_pages
                        and seen_run >= self.stop_on_seen_pages):
                    # 连续多页都没有新内容：增量模式下视为已追平
                    break
            else:
                seen_run = 0
            notify({"type": "page", "page": page, "got": got})
            page += 1
            if page % PAGES_PER_REST == 1 and page > 1:
                time.sleep(random.uniform(*REST_DELAY))
            else:
                time.sleep(random.uniform(*PAGE_DELAY))

    def crawl_all(self, store, on_progress=None) -> Dict:
        """采集全量并写入 store，返回摘要。"""
        user = self.fetch_user_info()
        self.expected_total = user["statuses_count"]
        store.save_user(user)
        if on_progress:
            on_progress({"type": "user", "user": user})
        count = 0
        for wb in self.iter_weibos(on_progress=on_progress):
            store.save_weibo(wb)
            count += 1
        return {"user": user, "count": count}
