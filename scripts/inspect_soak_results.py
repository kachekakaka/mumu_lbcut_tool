import json
import subprocess
from pathlib import Path

def main():
    ffprobe = r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE"
    run_dir = Path(r"..\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\20261104-e2e-30min-soak\run_1793776290_904dd7")

    print(f"=== 检查压测目录: {run_dir.resolve()} ===")
    for f in sorted(run_dir.iterdir()):
        if f.suffix in [".mp4", ".corrupted"]:
            cmd = [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(f)]
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
                data = json.loads(res.stdout)
                duration = data.get("format", {}).get("duration", "N/A")
                bitrate = data.get("format", {}).get("bit_rate", "N/A")
                size_mb = f.stat().st_size / (1024 * 1024)
                streams_info = []
                for s in data.get("streams", []):
                    c_type = s.get("codec_type")
                    c_name = s.get("codec_name")
                    if c_type == "video":
                        streams_info.append(f"视频: {c_name} {s.get('width')}x{s.get('height')} @ {s.get('r_frame_rate')} fps")
                    elif c_type == "audio":
                        streams_info.append(f"音频: {c_name} {s.get('sample_rate')}Hz {s.get('channels')}ch")
                print(f"\n【产物】{f.name}")
                print(f"  文件大小: {size_mb:.2f} MB")
                print(f"  媒体时长: {float(duration):.2f} 秒" if duration != "N/A" else f"  媒体时长: N/A")
                print(f"  码率: {int(bitrate)//1000} kbps" if bitrate != "N/A" else f"  码率: N/A")
                print(f"  轨道详情: {', '.join(streams_info)}")
            except Exception as e:
                print(f"\n【产物】{f.name}: 探测失败 -> {e}")

if __name__ == "__main__":
    main()
