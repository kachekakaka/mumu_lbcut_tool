from pathlib import Path
import subprocess
import cv2
import numpy as np

p = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\gate_verify\record_20261101_122023_seg_001.mp4")
print(f"分析文件: {p.name}, 大小: {p.stat().st_size / 1024 / 1024:.2f} MB")

for sec in ["00:00:03", "00:00:10", "00:00:18"]:
    out_img = Path(f"scripts/gate_test_frame_{sec.replace(':', '_')}.jpg")
    cmd = ["ffmpeg", "-y", "-ss", sec, "-i", str(p), "-vframes", "1", "-q:v", "2", str(out_img)]
    subprocess.run(cmd, capture_output=True)
    if out_img.exists() and out_img.stat().st_size > 0:
        img = cv2.imread(str(out_img))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        white_ratio = np.mean(gray > 240)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.mean(edges > 0)
        mean_bgr = img.mean(axis=(0, 1))
        print(f"时间 {sec} -> 白底率: {white_ratio:.1%}, 细节密度: {edge_density:.4f}, 平均颜色: {[round(c, 1) for c in mean_bgr]}")
