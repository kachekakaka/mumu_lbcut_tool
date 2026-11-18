from pathlib import Path
import subprocess
import json
from PIL import Image
import numpy as np

tmp_root = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp")
frame_dir = tmp_root / "extracted_frames"
frame_dir.mkdir(parents=True, exist_ok=True)

mp4_files = sorted(tmp_root.rglob("*.mp4"))
print(f"找到 {len(mp4_files)} 个 MP4 文件，开始抽帧分析...")

results = []

for idx, p in enumerate(mp4_files):
    if "extracted_frames" in str(p):
        continue
    out_img = frame_dir / f"{p.parent.name}_{p.stem}_frame.jpg"
    
    # 抽取第 5 秒的帧（如果视频太短就抽第 0.5 秒）
    cmd = [
        "ffmpeg", "-y",
        "-ss", "00:00:03",
        "-i", str(p),
        "-vframes", "1",
        "-q:v", "2",
        str(out_img)
    ]
    res = subprocess.run(cmd, capture_output=True)
    if not out_img.exists() or out_img.stat().st_size == 0:
        # 尝试第 0.5 秒
        cmd[2] = "00:00:00.5"
        subprocess.run(cmd, capture_output=True)

    if out_img.exists() and out_img.stat().st_size > 0:
        img = Image.open(out_img).convert("RGB")
        arr = np.array(img)
        # 登录界面特征：白色为主（大量像素 > 240, 240, 240），并且中间包含抖音粉红按钮 (R > 230, G < 80, B < 120)
        white_ratio = np.mean((arr[:, :, 0] > 235) & (arr[:, :, 1] > 235) & (arr[:, :, 2] > 235))
        pink_ratio = np.mean((arr[:, :, 0] > 220) & (arr[:, :, 1] < 100) & (arr[:, :, 2] < 140))
        mean_color = arr.mean(axis=(0, 1)).tolist()

        is_login = white_ratio > 0.4 and pink_ratio > 0.005
        status_label = "🔴 登录界面" if is_login else ("🟢 疑似直播/动态画面" if white_ratio < 0.3 else "🟡 待定")

        results.append({
            "dir": p.parent.name,
            "file": p.name,
            "size_mb": round(p.stat().st_size / (1024 * 1024), 2),
            "white_ratio": round(float(white_ratio), 3),
            "pink_ratio": round(float(pink_ratio), 4),
            "mean_color": [round(c, 1) for c in mean_color],
            "conclusion": status_label,
            "frame_path": str(out_img)
        })
        print(f"[{idx+1}/{len(mp4_files)}] {p.parent.name}/{p.name} -> {status_label} (白:{white_ratio:.2f}, 粉:{pink_ratio:.4f})")
    else:
        print(f"[{idx+1}/{len(mp4_files)}] {p.parent.name}/{p.name} -> 抽帧失败")

report_json = frame_dir / "summary.json"
with open(report_json, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

print("\n分析完成，结果已保存至 summary.json")
