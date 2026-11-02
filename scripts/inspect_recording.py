"""使用 ffprobe 检查录像切片的音视频流参数及音画对齐质量。"""
import json
from pathlib import Path
import subprocess
import sys


def inspect_file(video_path: Path) -> dict:
    if not video_path.exists():
        return {"error": f"文件不存在: {video_path}"}

    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(video_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        return {"error": f"ffprobe 分析失败: {res.stderr}"}

    data = json.loads(res.stdout)
    streams = data.get("streams", [])
    format_info = data.get("format", {})

    v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    v_dur = float(v_stream.get("duration", format_info.get("duration", 0))) if v_stream else 0.0
    a_dur = float(a_stream.get("duration", format_info.get("duration", 0))) if a_stream else 0.0
    drift_ms = abs(v_dur - a_dur) * 1000.0 if (v_stream and a_stream) else None

    report = {
        "file": str(video_path),
        "size_bytes": video_path.stat().st_size,
        "format_name": format_info.get("format_name"),
        "duration_sec": float(format_info.get("duration", 0)),
        "has_video": v_stream is not None,
        "video": {
            "codec": v_stream.get("codec_name"),
            "width": v_stream.get("width"),
            "height": v_stream.get("height"),
            "fps": v_stream.get("r_frame_rate"),
            "duration": v_dur,
        } if v_stream else None,
        "has_audio": a_stream is not None,
        "audio": {
            "codec": a_stream.get("codec_name"),
            "sample_rate": a_stream.get("sample_rate"),
            "channels": a_stream.get("channels"),
            "duration": a_dur,
        } if a_stream else None,
        "av_drift_ms": round(drift_ms, 2) if drift_ms is not None else None,
    }
    return report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python scripts/inspect_recording.py <mp4_path>")
        sys.exit(1)
    rep = inspect_file(Path(sys.argv[1]))
    print(json.dumps(rep, indent=2, ensure_ascii=False))
