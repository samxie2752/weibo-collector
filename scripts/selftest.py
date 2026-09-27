#!/usr/bin/env python3
"""离线自测：mock 掉 HTTP 层，验证解析、翻页、去重、验证码异常、存储全链路。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import crawler  # noqa: E402
from app.store import Store  # noqa: E402

PASS = FAIL = 0
def check(name, cond):
    global PASS, FAIL
    print(("  ✅ " if cond else "  ❌ ") + name)
    PASS, FAIL = PASS + cond, FAIL + (not cond)


# ---------- 1. 纯函数 ----------

print("[1] 解析函数")
check("html_to_text 去标签+br换行",
      crawler.html_to_text("abc<br>def<a href='#'>ghi</a>") == "abc\ndefghi")
check("html_to_text 实体反转义",
      crawler.html_to_text("A&amp;B&nbsp;C") == "A&B C")
check("string_to_int 万", crawler.string_to_int("1.2万") == 12000)
check("string_to_int 亿+", crawler.string_to_int("3亿+") == 300000000)
check("string_to_int 整数", crawler.string_to_int(123) == 123)
check("standardize_date 美式",
      crawler.standardize_date("Thu Jan 01 12:30:00 +0800 2020") == "2020-01-01 12:30:00")
check("standardize_date 昨天带时间",
      crawler.standardize_date("昨天 12:00").endswith(" 12:00:00"))

# ---------- 2. mock 采集全流程 ----------

print("[2] mock 采集流程")

MB = lambda bid, date, text="内容", **kw: {  # noqa: E731
    "id": bid, "created_at": date, "text": text, "source": '<a href="#">iPhone</a>',
    "reposts_count": "1.2万", "comments_count": 5, "attitudes_count": 10,
    "isLongText": kw.get("long", False), "mblogtype": kw.get("pinned", 0),
    "pic_num": 0,
    **({"retweeted_status": {"id": "RT1", "user": {"screen_name": "源"},
                             "text": "源文", "created_at": "Thu Jan 01 10:00:00 +0800 2020"}}
       if kw.get("rt") else {}),
}

PAGE1 = {"ok": 1, "data": {"cards": [
    {"card_type": 9, "mblog": MB("B2", "Thu Jan 02 12:00:00 +0800 2020")},
    {"card_type": 11, "card_group": [{"card_type": 9,
        "mblog": MB("B1", "Thu Jan 01 12:00:00 +0800 2020", rt=True)}]},
    {"card_type": 9, "mblog": MB("BP", "Thu May 05 12:00:00 +0800 2025", pinned=2)},
]}}
PAGE2 = {"ok": 1, "data": {"cards": [
    {"card_type": 9, "mblog": MB("B0", "Thu Jan 01 08:00:00 +0800 2019")},  # 早于 since
    {"card_type": 9, "mblog": MB("B1", "Thu Jan 01 12:00:00 +0800 2020")},  # 重复 id
]}}

USER = {"ok": 1, "data": {"userInfo": {
    "screen_name": "测试号", "gender": "m", "description": "简介",
    "statuses_count": 2, "followers_count": "3.4万", "follow_count": 100,
    "verified": True, "profile_image_url": "x.jpg"}}}

c = crawler.WeiboCollector("123", "SUB=abc", since_date="2020-01-01")
def fake_get(params):
    cid = params["containerid"]
    if cid.startswith("100505"):
        return USER
    if "_-_INFO" in cid:
        return {"ok": 0}
    return PAGE1 if params["page"] == 1 else PAGE2
c._get_json = fake_get

user = c.fetch_user_info()
check("用户解析：昵称/总数/粉丝数",
      user["screen_name"] == "测试号" and user["statuses_count"] == 2
      and user["followers_count"] == 34000)

wb_list = list(c.iter_weibos())
check("翻页采到 3 条（B2/B1转发/置顶BP，B0早于since且B1重复）", len(wb_list) == 3)
check("日期标准化", wb_list[0]["created_at"] == "2020-01-02 12:00:00")
check("转发识别 + 源拍平",
      wb_list[1]["is_retweet"] == 1 and wb_list[1]["retweet_user"] == "源"
      and wb_list[1]["retweet_created_at"] == "2020-01-01 10:00:00")
check("来源去 HTML", wb_list[0]["source"] == "iPhone")
check("计数万转数字", wb_list[0]["reposts_count"] == 12000)
check("置顶识别", any(w["is_pinned"] for w in wb_list))

# ---------- 3. 存储读写 ----------

print("[3] 存储")
tmp = tempfile.mkdtemp()
import app.store as store_mod  # noqa: E402
store_mod.DATA_DIR = tmp
s = Store("123")
s.save_user(user)
for w in wb_list:
    s.save_weibo(w)
s.save_weibo(wb_list[0])  # 幂等：重复写
check("计数（去重）", s.count_weibos() == 3)
check("分页查询第1页", len(s.list_weibos(1, 1)) == 1
      and s.list_weibos(1, 1)[0]["created_at"] == "2025-05-05 12:00:00")
check("用户读回", s.get_user()["screen_name"] == "测试号")
csvp = s.export_csv()
import csv as _csv  # noqa: E402
with open(csvp, encoding="utf-8-sig") as f:
    rows = list(_csv.reader(f))
check("CSV 导出行数（表头+3条）", len(rows) == 4)
s.close()

# ---------- 4. 验证码异常 ----------

print("[4] 风控/验证码异常")
class FakeResp:
    def __init__(self, js): self._js = js
    def json(self): return self._js
    def raise_for_status(self): pass

class FakeSession:
    def __init__(self, js): self._js = js
    def get(self, url, **kw): return FakeResp(self._js)

c2 = crawler.WeiboCollector("456", "SUB=x")
c2.session = FakeSession({"ok": -100, "url": "https://passport.weibo.com/sso/signin?x=1"})
try:
    c2.fetch_user_info()
    check("触发登录墙抛异常", False)
except crawler.NeedCaptchaError as e:
    check("真实 _get_json 路径抛 NeedCaptchaError，带 url",
          "passport.weibo.com" in e.url)

# ---------- 5. 断点续采 ----------

print("[5] 断点续采")
PAGE_A = {"ok": 1, "data": {"cards": [  # 整页已见（第3页，上次中断处附近）
    {"card_type": 9, "mblog": MB("B2", "Thu Jan 02 12:00:00 +0800 2020")},
    {"card_type": 9, "mblog": MB("B1", "Thu Jan 01 12:00:00 +0800 2020")},
]}}
PAGE_B = {"ok": 1, "data": {"cards": [  # 未见内容
    {"card_type": 9, "mblog": MB("B5", "Thu Jan 01 06:00:00 +0800 2020")},
]}}
calls = []
def fake_resume(params):
    calls.append(params["page"])
    if params["page"] == 3:
        return PAGE_A
    if params["page"] == 4:
        return PAGE_B
    return {"ok": 1, "data": {"cards": []}}  # 第5页起为空 → 结束

c3 = crawler.WeiboCollector("123", "SUB=abc", start_page=3, stop_on_seen_pages=0)
c3.seen_ids = {"B2", "B1", "BP"}
c3._get_json = fake_resume
resumed = list(c3.iter_weibos())
check("续采模式：整页已见不停（第3页跳过），从第4页采到新内容",
      [w["bid"] for w in resumed] == ["B5"] and calls == [3, 4, 5])

seen_calls = []
def fake_all_seen(params):
    seen_calls.append(params["page"])
    return PAGE_A  # 每页都已见

c4 = crawler.WeiboCollector("123", "SUB=abc", start_page=1, stop_on_seen_pages=2)
c4.seen_ids = {"B2", "B1"}
c4._get_json = fake_all_seen
inc = list(c4.iter_weibos())
check("增量模式：连续2页无新内容即追平停止",
      inc == [] and len(seen_calls) == 2)

# ---------- 6. 监控 check_once ----------

print("[6] 监控单轮检查")
from app import monitor as monitor_mod  # noqa: E402

s2 = Store("777")
s2.save_weibo(dict(wb_list[0], user_id="777"))  # 库里已有 B2


class MockCollector:
    stop_on_seen_pages = 0
    seen_ids = set()

    def iter_weibos(self):
        self.seen_ids_snapshot = set(self.seen_ids)
        yield dict(wb_list[1], user_id="777")  # 新微博 B1
        yield dict(wb_list[2], user_id="777")  # 置顶 BP（库里没有，也算新）


mc = MockCollector()
new = monitor_mod.check_once("777", "SUB=x", store=s2, collector=mc)
check("check_once 新微博入库并返回条数", new == 2 and s2.count_weibos() == 3)

# 生产路径：collector=None 时自行构造采集器并加载库内已见 id
class FakeCtor:
    last = None
    def __init__(self, uid, cookie, stop_on_seen_pages=0):
        self.stop_on_seen_pages = stop_on_seen_pages
        self.seen_ids = set()
    def iter_weibos(self):
        FakeCtor.last = (set(self.seen_ids), self.stop_on_seen_pages)
        return iter([])

orig_ctor = monitor_mod.crawler.WeiboCollector
monitor_mod.crawler.WeiboCollector = FakeCtor
try:
    new2 = monitor_mod.check_once("777", "SUB=x", store=s2)
finally:
    monitor_mod.crawler.WeiboCollector = orig_ctor
seen, stop = FakeCtor.last
check("check_once 生产路径：加载已见id + 增量模式",
      new2 == 0 and {"B2", "B1", "BP"} <= seen and stop == 2)
check("check_once 设置增量追平参数", monitor_mod.SEEN_STOP_PAGES == 2)
check("cookie 路径在 data/ 下", monitor_mod.cookie_path("777").endswith("777/cookie.txt"))
s2.close()

# ---------- 7. 防封加固 ----------

print("[7] 防封加固")
import time as _time  # noqa: E402
from app import tasks as tasks_mod  # noqa: E402

check("冷却公式：0 次不冷却", tasks_mod.resume_cooldown(0) == 0)
check("冷却公式：1 次 120 秒", tasks_mod.resume_cooldown(1) == 120)
check("冷却公式：3 次 480 秒", tasks_mod.resume_cooldown(3) == 480)
check("冷却公式：封顶 900 秒", tasks_mod.resume_cooldown(10) == 900)

crawler.GLOBAL_MIN_GAP = 0.3
crawler.WeiboCollector._last_req = 0.0
t0 = _time.time()
crawler.WeiboCollector._throttle()
crawler.WeiboCollector._throttle()
gap = _time.time() - t0
crawler.GLOBAL_MIN_GAP = 3.0
check("全局节流强制请求间隔", gap >= 0.25)

c5 = crawler.WeiboCollector("999", "SUB=x")
h = c5._random_headers()
check("请求头轮换（UA/语言/Referer）",
      h["User-Agent"] in crawler.USER_AGENTS
      and h["Accept-Language"] in crawler.ACCEPT_LANGS
      and h["Referer"] in crawler.REFERERS)
check("Cookie 注入请求头", h["Cookie"] == "SUB=x")

# ---------- 8. 任务实时动作 ----------

print("[8] 任务实时动作")
t = tasks_mod.Task("888", "SUB=x")
t.action, t.action_until = "抓取第 3 页", 0
info = t.info()
check("info 携带 action/action_until",
      info["action"] == "抓取第 3 页" and "action_until" in info)
t.action, t.action_until = "风控冷却中", 9999999999
check("冷却时带倒计时截止", t.info()["action_until"] == 9999999999)

# ---------- 9. 续采防重复 ----------

print("[9] 续采防重复校验")
tm = tasks_mod.manager
t1 = tasks_mod.Task("773", "SUB=x")
tm.tasks[t1.id] = t1
t1.state = "running"
check("running_task_for 识别进行中任务", tm.running_task_for("773") is t1)
t1.state = "done"
check("已完成任务不算进行中", tm.running_task_for("773") is None)
t1.state = "queued"
check("排队中任务算进行中", tm.running_task_for("773") is t1)
del tm.tasks[t1.id]

# ---------- 10. 风控计数时间衰减 ----------

print("[10] 风控计数时间衰减")
import time as _t2  # noqa: E402
ebc = tasks_mod.effective_ban_count
now = str(int(_t2.time()))
check("0 次恒为 0", ebc(0, now) == 0)
check("无时间戳的历史记录清零（旧账自动赦免）", ebc(4, "") == 0)
check("刚触发：4 次不衰减", ebc(4, now) == 4)
check("过了 1 小时：4 次衰减为 2 次", ebc(4, str(int(_t2.time()) - 3600)) == 2)
check("过了 5 小时：清零", ebc(4, str(int(_t2.time()) - 5 * 1800)) == 0)
check("衰减不会变负数", ebc(1, str(int(_t2.time()) - 99999)) == 0)
check("衰减后冷却：2 次=4 分钟",
      tasks_mod.resume_cooldown(ebc(4, str(int(_t2.time()) - 3600))) == 240)

# ---------- 11. 浏览器登录 Cookie 捕获 ----------

print("[11] 浏览器登录 Cookie 捕获")
from app import browserlogin as bl  # noqa: E402

cookies = [
    {"name": "SUB", "value": "com_version", "domain": ".weibo.com"},
    {"name": "SUB", "value": "cn_version", "domain": ".weibo.cn"},
    {"name": "_T_WM", "value": "x", "domain": ".weibo.cn"},
]
check("cookie 头构建：weibo.cn 域优先",
      bl.build_cookie_header(cookies) == "SUB=cn_version; _T_WM=x")

class FakeResp:
    def __init__(self, js): self._js = js
    def json(self): return self._js

orig_get = bl.requests.get
bl.requests.get = lambda *a, **kw: FakeResp({"data": {"login": True, "uid": "12345"}})
check("check_login 登录态返回 (uid, True)", bl.check_login("SUB=x") == ("12345", True))
bl.requests.get = lambda *a, **kw: FakeResp({"data": {"login": False}})
check("check_login 访客返回 ('', True)——可跳过", bl.check_login("SUB=x") == ("", True))
def _boom(*a, **kw): raise RuntimeError("network down")
bl.requests.get = _boom
check("check_login 网络异常返回 ('', False)——下轮重试", bl.check_login("SUB=x") == ("", False))
bl.requests.get = orig_get

s = bl.BrowserLoginSession.__new__(bl.BrowserLoginSession)
s.sid, s.target_uid, s.inject_cookie = "t", "", ""
s.status, s.error, s.note, s.uid, s.cookie = "launching", "", "", "", ""
s._cancel = bl.threading.Event()
info = s.info()
check("会话 info 完整（含 note）",
      info["status"] == "launching" and "note" in info and "cookie" in info)
s.cancel()
check("cancel 置位取消标志", s._cancel.is_set())

# ---------- 12. 图片下载 ----------

print("[12] 图片下载")
import os as _os  # noqa: E402
from app import media as media_mod  # noqa: E402

check("扩展名解析：jpg 带查询串",
      media_mod.ext_from_url("https://wx1.sinaimg.cn/large/abc.jpg?&690") == "jpg")
check("扩展名解析：png", media_mod.ext_from_url("https://x.cn/a/b.png") == "png")
check("扩展名解析：webp", media_mod.ext_from_url("https://x.cn/a/b.webp") == "webp")
check("扩展名解析：无扩展名降级 jpg",
      media_mod.ext_from_url("https://x.cn/a/xyz") == "jpg")
check("扩展名解析：假扩展名降级 jpg",
      media_mod.ext_from_url("https://x.cn/a/xyz.php?id=1") == "jpg")
check("文件名规则",
      media_mod.media_filename("B1", 2, "https://x.cn/a.jpg?v=1") == "B1_2.jpg")

s3 = Store("888")
prow = dict(wb_list[0], user_id="888",
            pic_urls="https://x.cn/a.jpg, https://x.cn/b.png")
s3.save_weibo(prow)
s3.save_weibo(dict(wb_list[1], user_id="888", pic_urls=""))
d = media_mod.media_dir("888")
_os.makedirs(d, exist_ok=True)
first = _os.path.join(d, media_mod.media_filename("B2", 1, "https://x.cn/a.jpg"))
open(first, "wb").write(b"x" * 100)  # 第一张已存在
jobs = media_mod.collect_jobs("888", s3)
check("collect_jobs：跳过已有文件和无图微博，只缺 1 张",
      len(jobs) == 1 and jobs[0][0] == "B2" and jobs[0][1] == 2
      and jobs[0][2] == "https://x.cn/b.png")

class FakeImgResp:
    status_code = 200
    content = b"\xff\xd8" + b"y" * 2000
    headers = {"Content-Type": "image/jpeg"}

class FakeSession:
    def get(self, url, **kw): return FakeImgResp()

check("download_one 成功落盘",
      media_mod.download_one(FakeSession(), "https://x.cn/b.png", jobs[0][3])
      and _os.path.exists(jobs[0][3]))

class FakeSmallResp:
    status_code = 200
    content = b"tiny"
    headers = {"Content-Type": "image/jpeg"}

orig_retry = media_mod.RETRY
media_mod.RETRY = 0  # 失败路径不做退避等待
check("download_one 拒绝反盗链占位小图",
      media_mod.download_one(FakeSession(), "https://x.cn/c.jpg", "/tmp/never") is False)
media_mod.RETRY = orig_retry

check("find_media_file 定位已下载文件",
      media_mod.find_media_file("888", "B2", 2) == jobs[0][3]
      and media_mod.find_media_file("888", "B2", 1) == first)
st = media_mod.MediaState("888")
check("媒体状态 info 完整", st.info()["state"] == "running" and "total" in st.info())
s3.close()

# ---------- 13. Markdown 导出 ----------

print("[13] Markdown 导出")
s4 = Store("999")
mrow1 = dict(wb_list[0], user_id="999", created_at="2024-11-08 18:22:54",
             pic_urls="https://x.cn/a.jpg?x=1",
             bid="B1", edited=1)
mrow2 = dict(wb_list[1], user_id="999", created_at="2024-12-01 09:00:00",
             bid="B2", edited=0)
s4.save_weibo(mrow1)
s4.save_weibo(mrow2)
md_dir = _os.path.join(store_mod.DATA_DIR, "999", "media")
_os.makedirs(md_dir, exist_ok=True)
open(_os.path.join(md_dir, "B1_1.jpg"), "wb").write(b"img")
md_path = s4.export_markdown()
md = open(md_path, encoding="utf-8").read()
check("月份分节", "## 2024年11月" in md and "## 2024年12月" in md)
check("正文与时间小标题", "### 2024-11-08 18:22:54" in md and "内容" in md)
check("本地图片用相对路径", "![图1](media/B1_1.jpg)" in md)
check("数据行与编辑标记", "🔁 12000 · 💬 5 · 👍 10 · 来自 iPhone" in md
      and "*（已编辑）*" in md)
s4.close()

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(1 if FAIL else 0)
