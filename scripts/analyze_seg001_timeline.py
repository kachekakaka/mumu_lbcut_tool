import subprocess
from pathlib import Path
import cv2
import numpy as np

seg1_path = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\worker_live_active\record_20261101_000329_seg_001.mp4")
out_dir = Path("scripts/seg1_timeline")
out_dir.mkdir(parents=True, exist_ok=True)

# 抽取 00:01:00, 00:05:00, 00:10:00, 00:15:00, 00:20:00, 00:25:00, 00:29:00
timestamps = [
    ("00:00:30", "30s"),
    ("00:02:00", "2m"),
    ("00:05:00", "5m"),
    ("00:10:00", "10m"),
    ("00:15:00", "15m"),
    ("00:20:00", "20m"),
    ("00:25:00", "25m"),
    ("00:29:00", "29m"),
]

print(f"分析 {seg1_path.name} 的时间线...")
for ts, label in timestamps:
    out_img = out_dir / f"frame_{label}.jpg"
    cmd = [
        "ffmpeg", "-y",
        "-ss", ts,
        "-i", str(seg1_path),
        "-vframes", "1",
        "-q:v", "2",
        str(out_img)
    ]
    subprocess.run(cmd, capture_output=True)
    if out_img.exists() and out_img.stat().st_size > 0:
        img = cv2.imread(str(out_img))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        white_ratio = np.mean(gray > 240)
        mean_bgr = img.mean(axis=(0, 1))
        # 边缘复杂度
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.mean(edges > 0)
        is_login = white_ratio > 0.6
        status = "🔴 登录界面" if is_login else ("🟢 真实直播画面" if edge_density > 0.03 else "🟡 暗色")
        print(f"时间 {ts} ({label}) -> {status} (白底: {white_ratio:.1%}, 细节: {edge_density:.4f}, 平均色: {[round(c, 1) for c in mean_bgr]})")
