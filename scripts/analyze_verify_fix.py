import subprocess
from pathlib import Path
import cv2
import numpy as np

fix_path = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\worker_verify_fix\record_20261031_234716_seg_001.mp4")
out_img = Path("scripts/fix_seg1_f2.jpg")

cmd = ["ffmpeg", "-y", "-ss", "00:00:02", "-i", str(fix_path), "-vframes", "1", "-q:v", "2", str(out_img)]
subprocess.run(cmd, capture_output=True)

img = cv2.imread(str(out_img))
print("verify_fix_seg1 第2秒分析:")
print("平均色 (BGR):", img.mean(axis=(0, 1)))
print("最大值/最小值:", img.max(), img.min())
