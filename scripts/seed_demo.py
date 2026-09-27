#!/usr/bin/env python3
"""生成演示数据：创建一个虚构账号和几条示例微博，让你在不配置 Cookie 的情况下
体验浏览/导出等界面功能。数据全部虚构，可随时删除。

用法：
    .venv/bin/python scripts/seed_demo.py     # 写入演示账号（uid 1000000000）
删除：
    .venv/bin/python scripts/seed_demo.py --clean
或在页面"已采集账号"卡片里点删除。
"""
import sys
from datetime import datetime, timedelta

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from app.store import Store  # noqa: E402

DEMO_UID = "1000000000"

DEMO_WEIBOS = [
    ("这是一条示例微博，用于演示列表展示效果。#示例话题# 欢迎使用微博历史采集工具。",
     "iPhone 17 Pro", 12, 3, 45),
    ("演示长文本：采集器会把列表页的截断正文替换为 detail 页全文，"
     "日期格式统一为 YYYY-MM-DD HH:MM:SS，转发链拍平成独立字段，"
     "计数里的'1.2万'会转成整数。以上都是自动完成的。",
     "Android", 3, 1, 20),
    ("带图表示例：正文里记录了图片/视频的 URL（默认不下载媒体文件）。"
     "这里展示的数字全是虚构的。",
     "Weibo.com", 0, 0, 8),
]


def seed():
    s = Store(DEMO_UID)
    s.save_user({
        "id": DEMO_UID,
        "screen_name": "示例用户",
        "gender": "男",
        "description": "演示账号：本账号与数据均为虚构，仅用于界面预览",
        "statuses_count": len(DEMO_WEIBOS),
        "followers_count": 1024,
        "follow_count": 128,
        "verified": 0,
        "verified_reason": "",
        "profile_image_url": "",
        "avatar_hd": "",
        "registration_time": "2015-06-01",
        "birthday": "", "location": "示例省 示例市", "ip_location": "示例",
        "sunshine": "", "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    base = datetime.now() - timedelta(days=len(DEMO_WEIBOS))
    for i, (text, source, rp, cm, at) in enumerate(DEMO_WEIBOS):
        ts = (base + timedelta(days=i, hours=10 + i)).strftime("%Y-%m-%d %H:%M:%S")
        s.save_weibo({
            "bid": f"DEMO{i}", "user_id": DEMO_UID, "created_at": ts,
            "text": text, "source": source,
            "reposts_count": rp, "comments_count": cm, "attitudes_count": at,
            "is_retweet": 0, "retweet_bid": "", "retweet_user": "",
            "retweet_text": "", "retweet_created_at": "",
            "pic_urls": "", "video_url": "", "article_url": "",
            "location": "", "is_long": 0, "edited": 0, "is_pinned": 0,
        })
    s.close()
    print(f"✅ 演示账号已写入（uid {DEMO_UID}，{len(DEMO_WEIBOS)} 条虚构微博）")
    print("   启动服务后即可在页面上浏览/导出体验。删除见 --clean。")


def clean():
    import shutil
    import os
    from app.store import DATA_DIR
    p = os.path.join(DATA_DIR, DEMO_UID)
    shutil.rmtree(p, ignore_errors=True)
    print(f"✅ 演示账号已删除（{p}）")


if __name__ == "__main__":
    clean() if "--clean" in sys.argv else seed()
