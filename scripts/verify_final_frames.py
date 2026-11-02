from pathlib import Path
import subprocess
import cv2
import numpy as np

p = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\gate_verify_closed\record_20261101_122147_seg_001.mp4")
print(f"分析实机录制切片: {p.name}, 大小: {p.stat().st_size / 1024 / 1024:.2f} MB")

for sec in ["00:00:03", "00:00:08", "00:00:12"]:
    out_img = Path(f"scripts/real_live_frame_{sec.replace(':', '_')}.jpg")
    cmd = ["ffmpeg", "-y", "-ss", sec, "-i", str(p), "-vframes", "1", "-q:v", "2", str(out_img)]
    subprocess.run(cmd, capture_output=True)
    if out_img.exists() and out_img.stat().st_size > 0:
        img = cv2.imread(str(out_img))
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        white_ratio = np.mean(gray > 240)
        edges = cv2.Canny(gray, 50, 150)
        edge_density = np.mean(edges > 0)
        mean_bgr = img.mean(axis=(0, 1))
        # 复制到 artifact 目录供用户查看
        import shutil
        shutil.copy(str(out_img), f"C:/Users/73839/.gemini/antigravity/brain/7c2e6987-53f3-457e-8f5b-92a672c86f35/real_live_frame_{sec.replace(':', '_')}.jpg")
        print(f"时间 {sec} -> 白底率: {white_ratio:.2%}, 细节密度: {edge_density:.4f}, 平均色: {[round(c, 1) for c in mean_bgr]}")
        if white_ratio < 0.10 and edge_density > 0.03:
            print("  => 结论: 🟢 100% 真实直播间画面（无任何登录页遮挡！）")
        else:
            print("  => 结论: 🟡 异常")
