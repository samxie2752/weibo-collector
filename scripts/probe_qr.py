#!/usr/bin/env python3
"""探测微博 wap 扫码登录接口的真实行为。

流程假设：passport.weibo.com 二维码 image 接口 → verify 轮询 → crossdomain 落 cookie。
用法：.venv/bin/python scripts/probe_qr.py
（会真实打印二维码图片到终端文件，需要用微博 App 扫码才能走完全程；
只跑前两步不扫码也能验证接口可用性。）
"""
import base64
import json
import sys
import time

import requests

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 "
                   "Mobile/15E148 Safari/604.1"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://m.weibo.cn/",
})

IMG_URL = "https://passport.weibo.com/sso/v2/qrcode/image"
VERIFY_URL = "https://passport.weibo.com/sso/v2/qrcode/verify"


def step0_warmup():
    r = S.get("https://m.weibo.cn/login/", timeout=10, allow_redirects=True)
    print(f"[0] 登录页 http={r.status_code} final={r.url}")
    print("    cookies:", sorted(c.name for c in S.cookies))
    return r.status_code


def step1_image():
    variants = [
        {"entry": "wapsso", "size": "180", "color": "C23138"},
        {"entry": "wapsso", "size": 180},
    ]
    for params in variants:
        r = S.get(IMG_URL, params=params, timeout=10)
        print(f"\n[1] image 接口 params={params} http={r.status_code}")
        try:
            js = r.json()
        except ValueError:
            print("    非 JSON:", r.text[:200])
            continue
        print("    msg:", js.get("msg"), "| ret:", js.get("ret"))
        data = js.get("data") or {}
        keys = {k: (f"<base64 {len(str(v))}字符>" if k == "image" else v)
                for k, v in data.items()}
        print("    data:", json.dumps(keys, ensure_ascii=False)[:300])
        if js.get("msg") == "succ" and data.get("image"):
            print("    ✅ 二维码接口可用")
            return data
    return None


def step2_verify(qrid):
    print(f"\n[2] 轮询 verify（qrid={qrid[:20]}…）— 用微博 App 扫码可看到状态变化")
    for i in range(30):
        r = S.get(VERIFY_URL, params={"entry": "wapsso", "qrid": qrid},
                  timeout=10)
        try:
            js = r.json()
        except ValueError:
            print(f"    #{i} 非 JSON http={r.status_code}:", r.text[:150])
            time.sleep(2)
            continue
        ret, data = js.get("ret"), js.get("data") or {}
        print(f"    #{i} ret={ret} msg={js.get('msg')} data={json.dumps(data, ensure_ascii=False)[:200]}")
        if ret in ("20000000", 20000000):
            print("    ✅ 确认成功！")
            return data
        time.sleep(2)
    return None


def step3_crossdomain(data):
    url = data.get("url") or data.get("crossdomain_url")
    if not url:
        print("\n[3] 无 crossdomain url，看看返回里还有什么:", data)
        return
    print(f"\n[3] 访问 crossdomain: {url[:120]}")
    r = S.get(url, timeout=10, allow_redirects=True)
    print("    http=", r.status_code, "final=", r.url)
    print("    cookies:", sorted(c.name for c in S.cookies))
    has_sub = any(c.name == "SUB" for c in S.cookies)
    print("    ✅ 拿到 SUB！" if has_sub else "    ❌ 没有 SUB")
    if has_sub:
        cfg = S.get("https://m.weibo.cn/api/config", timeout=10).json()
        d = cfg.get("data") or {}
        print("    api/config: login=", d.get("login"), "uid=", d.get("uid"))


def main():
    step0_warmup()
    data = step1_image()
    if not data:
        print("\n结论：image 接口不可用，需要走 Playwright 备选方案")
        sys.exit(1)
    qrid = data.get("qrid") or data.get("qrcode_id") or data.get("id")
    polled = step2_verify(qrid)
    if polled:
        step3_crossdomain(polled)
    else:
        print("\n未扫码/超时——image+verify 接口本身可达即为好消息")


if __name__ == "__main__":
    main()
