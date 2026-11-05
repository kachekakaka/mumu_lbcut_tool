import json
import subprocess
from pathlib import Path
import numpy as np

ffprobe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.exe")
ffmpeg = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffmpeg.exe")

soak_mp4 = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\20261104-e2e-30min-soak\run_1793776290_904dd7\7704140426680273691_1793776965_0002_02.mp4")
df_mp4 = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\verify_e2e_overlap\run_1793791248_d91a74\7704073922697906979_1793791343_0001_01.mp4")

def analyze_stutter(mp4_path: Path):
    # 提取前 200 个视频帧时间戳
    cmd = [
        str(ffprobe),
        "-v", "quiet",
        "-select_streams", "v:0",
        "-show_frames",
        "-show_entries", "frame=pts_time,pkt_dts_time,duration_time",
        "-read_intervals", "0%+10",
        "-print_format", "json",
        str(mp4_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    frames = json.loads(res.stdout).get("frames", [])
    pts_list = [float(f["pts_time"]) for f in frames if "pts_time" in f and f["pts_time"] is not None]
    
    if len(pts_list) < 2:
        return {}
    
    deltas = np.diff(pts_list)
    return {
        "total_frames_10s": len(pts_list),
        "mean_fps": len(pts_list) / (pts_list[-1] - pts_list[0]),
        "mean_delta_ms": float(np.mean(deltas) * 1000),
        "min_delta_ms": float(np.min(deltas) * 1000),
        "max_delta_ms": float(np.max(deltas) * 1000),
        "std_delta_ms (抖动)": float(np.std(deltas) * 1000),
        "frames_over_100ms (卡顿帧)": int(np.sum(deltas > 0.100)),
        "frames_over_200ms (大卡顿)": int(np.sum(deltas > 0.200)),
    }

print("=== 30分钟长稳切片 (流畅) ===")
print(json.dumps(analyze_stutter(soak_mp4), indent=2))

print("\n=== 优化后东方甄选切片 (用户反馈迟钝) ===")
print(json.dumps(analyze_stutter(df_mp4), indent=2))
