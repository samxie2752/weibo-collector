#!/usr/bin/env python3
"""接口探活脚本：实测微博移动端 3 个核心接口是否可用。

用法：
    .venv/bin/python scripts/probe.py [user_id] [cookie]

不传 cookie 时测试匿名访问能力；传 cookie 时（从浏览器复制的整串 cookie）
测试登录态访问。user_id 不传则用默认测试账号。
"""
import json
import sys
from datetime import datetime

import requests

BASE = "https://m.weibo.cn/api/container/getIndex"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://m.weibo.cn/",
}


def make_session(cookie: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if cookie:
        s.headers["Cookie"] = cookie
    # 预热：先访问主页拿指纹 cookie
    try:
        s.get("https://m.weibo.cn/", timeout=10)
        got = {c.name for c in s.cookies}
        print(f"[预热] m.weibo.cn 主页 OK，获得 cookie: {sorted(got)}")
    except Exception as e:
        print(f"[预热] 失败：{e}")
    return s


def probe_user_info(s: requests.Session, uid: str):
    r = s.get(BASE, params={"containerid": "100505" + uid}, timeout=10)
    js = r.json()
    ok = js.get("ok")
    print(f"\n[1] 用户资料 containerid=100505{uid}  http={r.status_code} ok={ok}")
    if ok == 1:
        u = js["data"]["userInfo"]
        print(f"    昵称: {u.get('screen_name')}  微博数: {u.get('statuses_count')}  "
              f"粉丝: {u.get('followers_count')}  认证: {u.get('verified')}")
    else:
        print(f"    返回: {json.dumps(js, ensure_ascii=False)[:300]}")
    return js


def probe_posts_page(s: requests.Session, uid: str, count: int = 10):
    r = s.get(BASE, params={"containerid": "230413" + uid, "page": 1,
                            "count": count}, timeout=10)
    js = r.json()
    ok = js.get("ok")
    print(f"\n[2] 微博列表 containerid=230413{uid} page=1  http={r.status_code} ok={ok}")
    if ok == 1:
        cards = js.get("data", {}).get("cards", [])
        weibo_cards = [c for c in cards if c.get("card_type") in (9, 11)]
        print(f"    cards 总数: {len(cards)}，其中微博卡片(9/11): {len(weibo_cards)}")
        since_id = js.get("data", {}).get("cardlistInfo", {}).get("since_id", "")
        print(f"    since_id: {since_id!r}")
        for c in weibo_cards[:2]:
            mb = c.get("mblog", {})
            text = mb.get("text", "")[:40].replace("\n", " ")
            print(f"    - id={mb.get('id')}  {mb.get('created_at')}  "
                  f"long={mb.get('isLongText')}  赞{mb.get('attitudes_count')}  {text}")
        return weibo_cards
    print(f"    返回: {json.dumps(js, ensure_ascii=False)[:300]}")
    return []


def probe_long_text(s: requests.Session, weibo_cards):
    """找第一条长微博试 detail 接口"""
    target = None
    for c in weibo_cards:
        mb = c.get("mblog", {})
        if mb.get("isLongText") or (mb.get("pic_num") or 0) > 9:
            target = mb
            break
    if not target:
        print("\n[3] 第一页没有长微博，跳过 detail 测试")
        return
    wid = target["id"]
    r = s.get(f"https://m.weibo.cn/detail/{wid}", timeout=10)
    html = r.text
    print(f"\n[3] 长微博 detail/{wid}  http={r.status_code}  html长度={len(html)}")
    start = html.find('"status":')
    if start == -1:
        print("    未找到内嵌 JSON")
        return
    frag = html[start:html.rfind('"call"')]
    frag = frag[:frag.rfind(",")]
    try:
        js = json.loads("{" + frag + "}", strict=False)
        full = js.get("status", {}).get("text", "")
        print(f"    全文长度: {len(full)}  开头: {full[:60]!r}")
    except Exception as e:
        print(f"    JSON 解析失败: {e}")


def main():
    uid = sys.argv[1] if len(sys.argv) > 1 else "2803301701"  # 人民日报
    cookie = sys.argv[2] if len(sys.argv) > 2 else ""
    print(f"目标 uid: {uid}  cookie: {'有' if cookie else '无(匿名)'}  "
          f"时间: {datetime.now():%H:%M:%S}")
    s = make_session(cookie)
    try:
        probe_user_info(s, uid)
        cards = probe_posts_page(s, uid)
        probe_long_text(s, cards)
    except Exception as e:
        print(f"\n探活异常: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
