"""核验 10 分钟压测切片连续性与流健康度。"""
import json
import subprocess
from pathlib import Path

ffprobe_exe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE")
run_dir = Path(r"..\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\verify_batch_room\room_74400659583_bottom\run_1793842235_ff8e15")
files = sorted([f for f in run_dir.iterdir() if f.name.endswith(".mp4") and not f.name.startswith("record_")])

print(f"找到切片文件数量: {len(files)}")
total_dur = 0.0
for i, f in enumerate(files, 1):
    res = subprocess.run(
        [str(ffprobe_exe), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(f)],
        capture_output=True,
        text=True,
    )
    data = json.loads(res.stdout)
    fmt = data.get("format", {})
    dur = float(fmt.get("duration", 0))
    total_dur += dur
    br = int(fmt.get("bit_rate", 0)) // 1000
    streams = data.get("streams", [])
    v_codec = next((s["codec_name"] for s in streams if s["codec_type"] == "video"), None)
    a_codec = next((s["codec_name"] for s in streams if s["codec_type"] == "audio"), None)
    r_frame_rate = next((s["r_frame_rate"] for s in streams if s["codec_type"] == "video"), "0/0")
    num, den = map(int, r_frame_rate.split("/"))
    fps = num / den if den > 0 else 0
    print(f"切片 #{i} [{f.name}]: 时长={dur:.2f}s, 大小={f.stat().st_size/1024/1024:.2f}MB, 码率={br}kbps, 视频={v_codec}({fps:.2f}fps), 音频={a_codec}")

print(f"\n累计有效录制时长: {total_dur:.2f}s ({total_dur/60:.2f} 分钟)")
