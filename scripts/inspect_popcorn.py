import requests
import re
import json

url = "https://live.douyin.com/48859132415"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://live.douyin.com/",
}
resp = requests.get(url, headers=headers)
print("网页状态码:", resp.status_code)

# 搜索 19 位数字
ids = re.findall(r'77\d{17}', resp.text)
print("找到的 77 开头 19 位 room_id 候选:", set(ids))

# 搜索 roomId_str
room_ids = re.findall(r'\"roomId_str\"\s*:\s*\"(\d+)\"', resp.text)
print("roomId_str:", set(room_ids))

# 搜索 status
status_matches = re.findall(r'\"status\"\s*:\s*(\d+)', resp.text)
print("status 集合:", set(status_matches))
