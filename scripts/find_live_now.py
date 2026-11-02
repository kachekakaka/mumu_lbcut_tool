"""快速查找当前开播的优质直播间 ID。"""
import requests
import re
import json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://live.douyin.com/",
}

# 常见官方/高频开播直播间
candidates = [
    ("737141", "东方甄选"),
    ("7702849418285943592", "马上逃离地球"),
    ("7702808192775457590", "肉肉"),
    ("cctvnews", "央视新闻"),
    ("人民网", "人民网"),
    ("99999", "交个朋友"),
    ("283281031", "新东方"),
    ("1805903258", "优质主播A"),
    ("10001", "直播间1"),
]

print("正在检测在播房间...")
found = []
for rid, name in candidates:
    url = f"https://live.douyin.com/{rid}"
    try:
        resp = requests.get(url, headers=headers, timeout=3)
        html = resp.text
        m = re.search(r'\"status\"\s*:\s*(\d+)', html)
        status = int(m.group(1)) if m else -1
        m_room = re.search(r'\"roomId\"\s*:\s*\"?(\d+)\"?', html)
        real_room_id = m_room.group(1) if m_room else ""
        if status == 2:
            print(f"🟢 在播: {name} ({rid}) -> room_id={real_room_id}")
            found.append((rid, real_room_id, name))
        else:
            print(f"🔴 未开播: {name} ({rid}) status={status}")
    except Exception as e:
        print(f"检测失败: {name} ({e})")

print(f"\n找到在播房间数: {len(found)}")
