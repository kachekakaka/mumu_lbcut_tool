import cv2
import numpy as np
from pathlib import Path

artifact_dir = Path(r"C:\Users\73839\.gemini\antigravity\brain\7c2e6987-53f3-457e-8f5b-92a672c86f35")

images = [
    ("verify_fix_seg001", artifact_dir / "frame_verify_fix_seg001.jpg"),
    ("live_active_seg001", artifact_dir / "frame_live_active_seg001.jpg"),
    ("live_active_seg002", artifact_dir / "frame_live_active_seg002.jpg"),
]

for label, p in images:
    if not p.exists():
        continue
    img = cv2.imread(str(p))
    h, w, c = img.shape
    # 灰度图与边缘
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    edge_density = np.mean(edges > 0)
    
    # 色彩统计
    mean_bgr = img.mean(axis=(0, 1))
    std_bgr = img.std(axis=(0, 1))
    
    # 顶部 20% 是否为纯白
    top_white = np.mean(gray[: int(h * 0.2), :] > 240)
    # 中间 50% 是否为纯白
    mid_white = np.mean(gray[int(h * 0.25) : int(h * 0.75), :] > 240)
    
    # 检查是否有粉红色（抖音主色调验证码按钮）
    # BGR 中 R 高, G 低, B 低
    pink_mask = (img[:, :, 2] > 220) & (img[:, :, 1] < 120) & (img[:, :, 0] < 150)
    pink_count = np.sum(pink_mask)

    print(f"\n--- 分析样本: {label} ---")
    print(f"分辨率: {w}x{h}")
    print(f"平均颜色 (BGR): {[round(x, 1) for x in mean_bgr]}")
    print(f"色彩方差: {[round(x, 1) for x in std_bgr]}")
    print(f"边缘复杂度 (细节丰富度): {edge_density:.4f}")
    print(f"顶部白区比例: {top_white:.2%}, 中部白区比例: {mid_white:.2%}")
    print(f"粉红按钮像素数: {pink_count}")
    
    if mid_white > 0.6 and top_white > 0.6:
        print("=> 判定结果: 🔴 纯白登录页 / 引导遮罩")
    elif edge_density > 0.05 or std_bgr.mean() > 40:
        print("=> 判定结果: 🟢 真实直播间画面（色彩与细节丰富）")
    else:
        print("=> 判定结果: 🟡 暗色或占位画面")
