"""端到端验证：针对用户样本直播间进行真实切片录制，并分析真实物理帧率与视觉动作流畅度。"""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("VerifyBatchRoom")

FFPROBE_CANDIDATES = [
    Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE"),
    Path(r"C:\ProgramData\chocolatey\bin\ffprobe.exe"),
    Path(r"D:\soft\ffmpeg\bin\ffprobe.exe"),
]

def find_ffprobe() -> Path:
    for c in FFPROBE_CANDIDATES:
        if c.exists():
            return c
    res = subprocess.run(["where", "ffprobe"], capture_output=True, text=True)
    if res.returncode == 0:
        return Path(res.stdout.strip().splitlines()[0])
    raise FileNotFoundError("未找到 ffprobe.exe")

def analyze_motion_frames(video_path: Path, max_frames: int = 150):
    cap = cv2.VideoCapture(str(video_path))
    reported_fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    prev_frame = None
    diffs = []
    identical_count = 0
    read_frames = 0
    
    while read_frames < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
        read_frames += 1
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if prev_frame is not None:
            diff = cv2.absdiff(prev_frame, gray)
            mean_diff = float(np.mean(diff))
            diffs.append(mean_diff)
            if mean_diff < 0.5:
                identical_count += 1
        prev_frame = gray
    cap.release()
    
    unique_rate = 1.0 - (identical_count / max(1, len(diffs)))
    real_motion_fps = reported_fps * unique_rate
    return {
        "reported_fps": reported_fps,
        "total_frames": total_frames,
        "read_frames": read_frames,
        "mean_diff": np.mean(diffs) if diffs else 0.0,
        "identical_frames": identical_count,
        "unique_ratio": unique_rate,
        "real_motion_fps": real_motion_fps,
    }

def probe_mp4(ffprobe_exe: Path, mp4_path: Path) -> dict:
    cmd = [
        str(ffprobe_exe),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(mp4_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    data = json.loads(res.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))
    size_bytes = int(data.get("format", {}).get("size", 0))
    video_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
    audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    nb_frames = int(video_streams[0].get("nb_frames", 0)) if (video_streams and video_streams[0].get("nb_frames")) else 0
    real_fps = (nb_frames / duration) if (duration > 0 and nb_frames > 0) else 0.0
    bitrate = int(data.get("format", {}).get("bit_rate", 0)) // 1000

    return {
        "duration": duration,
        "size_mb": size_bytes / (1024 * 1024),
        "bitrate_kbps": bitrate,
        "video_codec": video_streams[0].get("codec_name") if video_streams else None,
        "audio_codec": audio_streams[0].get("codec_name") if audio_streams else None,
        "has_video": len(video_streams) > 0,
        "has_audio": len(audio_streams) > 0,
        "nb_frames": nb_frames,
        "real_fps": round(real_fps, 2),
    }

def run_test(
    target_entry: str = "852703780",
    background: bool = False,
    window_mode: str = "foreground",
    pos_x: int | None = None,
    pos_y: int | None = None,
    segment_duration: int = 25,
    total_duration: int = 55,
):
    ffprobe_exe = find_ffprobe()
    logger.info(f"使用 ffprobe: {ffprobe_exe}")

    eff_mode = window_mode
    if background and eff_mode == "foreground":
        eff_mode = "hidden"

    safe_target = "".join(c for c in target_entry if c.isalnum() or c in ("_", "-"))
    mode_str = f"{eff_mode}"
    out_base = Path(r"..\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\verify_batch_room") / f"room_{safe_target}_{mode_str}"
    out_base.mkdir(parents=True, exist_ok=True)

    import shutil
    for p in out_base.glob("run_*"):
        if p.is_dir():
            try:
                shutil.rmtree(p)
            except Exception:
                pass

    cmd = [
        sys.executable,
        "-m", "src.worker.cli",
        "--vmindex", "2",
        "--target", target_entry,
        "--output-dir", str(out_base),
        "--segment-duration", str(segment_duration),
        "--total-duration", str(total_duration),
        "--allow-unconfirmed-identity",
        "--window-mode", eff_mode,
    ]
    if pos_x is not None:
        cmd.extend(["--window-pos-x", str(pos_x)])
    if pos_y is not None:
        cmd.extend(["--window-pos-y", str(pos_y)])

    env = dict(os.environ, PYTHONPATH=".")
    logger.info(f"启动 Worker (模式: {mode_str}, 总时长: {total_duration}s, 切片: {segment_duration}s) 录制样本房间 {target_entry}: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    import threading
    stderr_lines = []
    def _drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
            logger.debug(f"[Worker Stderr] {line.strip()}")

    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_err.start()

    events = []
    start_time = time.time()
    while True:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            break
        if line:
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    ev = json.loads(stripped)
                    events.append(ev)
                    ev_type = ev.get("event", "unknown")
                    if ev_type in ("segment_started", "segment_finished", "segment_aborted", "login_blocked"):
                        logger.info(f"[Worker Event] >>> {stripped}")
                except Exception:
                    pass

    exit_code = proc.wait()
    t_err.join(timeout=2)
    logger.info(f"Worker 进程退出，退出码: {exit_code} (总耗时: {time.time() - start_time:.1f}s)")

    finished_events = [e for e in events if e.get("event") == "segment_finished"]
    finished_files = [e.get("file_path") for e in finished_events]
    logger.info(f"已录制完成切片文件: {finished_files}")

    assert len(finished_files) >= 1, "未生成有效录制切片！"

    for idx, f_path in enumerate(finished_files, start=1):
        p = Path(f_path)
        if not p.exists():
            continue
        p_info = probe_mp4(ffprobe_exe, p)
        m_info = analyze_motion_frames(p, max_frames=150)
        logger.info(f"==================================================")
        logger.info(f"【切片 #{idx} 分析结果】{p.name}")
        logger.info(f"  时长: {p_info['duration']:.2f}s | 大小: {p_info['size_mb']:.2f}MB | 码率: {p_info['bitrate_kbps']} kbps")
        logger.info(f"  容器帧率: {p_info['real_fps']:.2f} fps (总帧数: {p_info['nb_frames']})")
        logger.info(f"  帧间平均差异度: {m_info['mean_diff']:.2f}")
        logger.info(f"  独立动作帧占比: {m_info['unique_ratio']:.2%}")
        logger.info(f"  真实有效动作 FPS: {m_info['real_motion_fps']:.2f} 动作变化/秒")
        logger.info(f"==================================================")

    # 提取预览关键帧
    preview_dir = Path(r"C:\Users\73839\.gemini\antigravity\brain\bd946f40-a1fa-4dce-a8b1-c9f8b8739927\preview_frames")
    preview_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg_exe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffmpeg.exe")
    if ffmpeg_exe.exists() and len(finished_files) >= 1:
        subprocess.run([str(ffmpeg_exe), "-y", "-ss", "00:00:05", "-i", finished_files[0], "-vframes", "1", str(preview_dir / "batch_seg01.jpg")], capture_output=True)
        if len(finished_files) >= 2:
            subprocess.run([str(ffmpeg_exe), "-y", "-ss", "00:00:05", "-i", finished_files[1], "-vframes", "1", str(preview_dir / "batch_seg02.jpg")], capture_output=True)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="端到端验证切片录制与帧率分析")
    parser.add_argument("target", nargs="?", default="852703780", help="直播间标识")
    parser.add_argument("--background", action="store_true", default=False, help="后台静默隐藏模式")
    parser.add_argument("--window-mode", choices=["foreground", "bottom", "offscreen", "hidden"], default="foreground")
    parser.add_argument("--window-pos-x", type=int, default=None)
    parser.add_argument("--window-pos-y", type=int, default=None)
    parser.add_argument("--segment-duration", type=int, default=25, help="单切片时长(秒)")
    parser.add_argument("--total-duration", type=int, default=55, help="总录制时长(秒)")
    parsed = parser.parse_args()

    mode = "hidden" if parsed.background else parsed.window_mode
    run_test(
        target_entry=parsed.target,
        background=parsed.background,
        window_mode=mode,
        pos_x=parsed.window_pos_x,
        pos_y=parsed.window_pos_y,
        segment_duration=parsed.segment_duration,
        total_duration=parsed.total_duration,
    )
