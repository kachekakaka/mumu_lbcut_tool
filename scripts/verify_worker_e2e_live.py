"""模拟器实机端到端验证脚本：
验证 Worker 纯离线启动、推流录制、Windows 中断信号 (CTRL_BREAK_EVENT) 平滑封口、
is_final: true 标记与退出码 0。
"""
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("WorkerLiveE2E")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TMP_OUTPUT_DIR = (
    REPO_ROOT.parent / "mumu_lbcut_tool_dev" / "mumu_lbcut_tool_tmp" / "e2e_live_verify"
)


FFPROBE_CANDIDATES = [
    Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE"),
    Path(r"C:\ProgramData\chocolatey\bin\ffprobe.exe"),
    Path(r"D:\soft\ffmpeg\bin\ffprobe.exe"),
]

FFMPEG_CANDIDATES = [
    Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffmpeg.EXE"),
    Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
    Path(r"D:\soft\ffmpeg\bin\ffmpeg.exe"),
]


def find_binary(candidates: list[Path], name: str) -> Path:
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which(name)
    if found:
        return Path(found)
    raise FileNotFoundError(f"未找到二进制文件: {name}")


def probe_mp4(ffprobe_exe: Path, mp4_path: Path) -> dict:
    """使用 ffprobe 深入探测 MP4 完整性与音画轨。"""
    cmd = [
        str(ffprobe_exe),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(mp4_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe 检测失败 ({mp4_path.name}): {res.stderr}")
    data = json.loads(res.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    duration = float(fmt.get("duration", 0.0))
    size_bytes = int(fmt.get("size", 0))
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "file_name": mp4_path.name,
        "duration": duration,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "video_codec": video_streams[0].get("codec_name") if video_streams else None,
        "audio_codec": audio_streams[0].get("codec_name") if audio_streams else None,
        "width": video_streams[0].get("width") if video_streams else 0,
        "height": video_streams[0].get("height") if video_streams else 0,
        "has_video": len(video_streams) > 0,
        "has_audio": len(audio_streams) > 0,
    }


def extract_preview_frame(ffmpeg_exe: Path, mp4_path: Path, output_jpg: Path, time_sec: int = 5):
    """从 MP4 文件截取关键帧用于验证画面质量。"""
    output_jpg.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(ffmpeg_exe),
        "-y",
        "-ss", str(time_sec),
        "-i", str(mp4_path),
        "-vframes", "1",
        str(output_jpg),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if res.returncode != 0:
        logger.warning(f"提取关键帧失败: {res.stderr}")
    else:
        logger.info(f"成功保存预览帧: {output_jpg}")


def run_e2e_test(
    vmindex: int = 2,
    target: str = "48859132415",
    segment_duration: int = 40,
    record_seconds: int = 65,
    window_mode: str = "bottom",
):
    ffprobe_exe = find_binary(FFPROBE_CANDIDATES, "ffprobe")
    ffmpeg_exe = find_binary(FFMPEG_CANDIDATES, "ffmpeg")
    logger.info(f"依赖二进制检查通过: ffprobe={ffprobe_exe}, ffmpeg={ffmpeg_exe}")

    # 每次验证独立目录，绝不删除以前的 run_* 录像或证据。
    run_dir = TMP_OUTPUT_DIR / (time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex)
    run_dir.mkdir(parents=True, exist_ok=False)
    preview_dir = run_dir / "preview"

    cmd = [
        sys.executable,
        "-m", "src.worker.cli",
        "--vmindex", str(vmindex),
        "--target", target,
        "--output-dir", str(run_dir),
        "--segment-duration", str(segment_duration),
        "--window-mode", window_mode,
        "--allow-unconfirmed-identity",
    ]

    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), PYTHONUNBUFFERED="1")
    logger.info(f"启动 Worker CLI 进程: {' '.join(cmd)}")

    # 在 Windows 下开启 CREATE_NEW_PROCESS_GROUP，以便 os.kill(pid, CTRL_BREAK_EVENT) 独立分发
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
    )

    stderr_lines: list[str] = []

    def _drain_stderr():
        for line in proc.stderr:
            stderr_lines.append(line)
            # 记录关键 worker 调试信息
            stripped = line.strip()
            if any(k in stripped for k in ("Stopping", "Signal", "Finalizing", "Cleaned", "Error", "Exception")):
                logger.debug(f"[Worker Stderr] {stripped}")

    t_err = threading.Thread(target=_drain_stderr, daemon=True)
    t_err.start()

    events: list[dict] = []
    first_segment_started = threading.Event()

    def _drain_stdout():
        for line in proc.stdout:
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    data = json.loads(stripped)
                    events.append(data)
                    ev_type = data.get("event")
                    if ev_type == "segment_finished":
                        logger.info(
                            f"[NDJSON Event] 🎬 segment_finished: {Path(data.get('file_path', '')).name} "
                            f"(is_final={data.get('is_final')}, duration={data.get('duration_sec', 0):.1f}s)"
                        )
                    elif ev_type == "segment_started":
                        logger.info(f"[NDJSON Event] 🚀 segment_started: {Path(data.get('file_path', '')).name}")
                        first_segment_started.set()
                    elif ev_type == "heartbeat":
                        fps = data.get("fps", 0)
                        bytes_rec = data.get("file_size_mb", 0)
                        logger.info(f"[NDJSON Event] ❤️ heartbeat: fps={fps if fps is not None else 'unknown'}, size={bytes_rec:.2f}MB")
                    elif ev_type in ("worker_started", "worker_stopped", "worker_aborted"):
                        logger.info(f"[NDJSON Event] ℹ️ {ev_type}: {data}")
                except Exception:
                    pass

    t_out = threading.Thread(target=_drain_stdout, daemon=True)
    t_out.start()

    logger.info(f"Worker PID={proc.pid} 已拉起，等待模拟器初始化与首次 segment_started...")
    # 等待模拟器冷启动并启动首个切片（最多 90 秒）
    if not first_segment_started.wait(timeout=90):
        logger.error("超时未收到 segment_started 事件，模拟器或 Worker 启动可能异常！")
        proc.kill()
        raise TimeoutError("未收到首个切片启动事件")

    logger.info(f"首个切片已开始录制，计时观察 {record_seconds} 秒...")
    start_record_time = time.time()
    signaled = False
    try:
        while time.time() - start_record_time < record_seconds:
            if proc.poll() is not None:
                logger.error(f"Worker 异常提前退出，退出码: {proc.returncode}")
                break
            time.sleep(1)

        # 录制达到预期时长，发送 CTRL_BREAK_EVENT 信号
        if proc.poll() is None:
            logger.info(f"达到计划录制时长 ({record_seconds}s)，向 Worker 发送 CTRL_BREAK_EVENT 优雅停机信号...")
            if sys.platform == "win32":
                os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.kill(proc.pid, signal.SIGINT)
            signaled = True

        # 等待平滑停机完成（最多 25 秒安全封口窗口）
        logger.info("等待 Worker 平滑封口并正常退出...")
        exit_code = proc.wait(timeout=30)
        logger.info(f"Worker 进程已退出，退出码: {exit_code}")
    except subprocess.TimeoutExpired:
        logger.error("平滑停机超时，强制终止进程！")
        proc.kill()
        exit_code = proc.wait()
    except KeyboardInterrupt:
        logger.warning("收到外部中断，正在终止 Worker...")
        if proc.poll() is None:
            proc.kill()
        exit_code = proc.wait()
        raise

    t_out.join(timeout=3)
    t_err.join(timeout=3)

    logger.info("==================== 验收断言与分析 ====================")
    assert exit_code == 0, f"Worker 退出码必须为 0，实际为: {exit_code}"

    # 验证事件序列
    seg_finished_events = [e for e in events if e.get("event") == "segment_finished"]
    assert len(seg_finished_events) >= 1, f"至少应产出一个 segment_finished 事件，实际产出: {len(seg_finished_events)}"

    last_event = seg_finished_events[-1]
    logger.info(f"最终切片事件: {last_event}")
    assert last_event.get("is_final") is True, f"最后一个 segment_finished 事件的 is_final 必须为 True，实际为: {last_event.get('is_final')}"

    # 如果有多个切片，前面的切片 is_final 应该为 False
    if len(seg_finished_events) > 1:
        for idx, prev_ev in enumerate(seg_finished_events[:-1]):
            assert prev_ev.get("is_final") is False, f"中间切片 #{idx+1} 的 is_final 必须为 False，实际为: {prev_ev.get('is_final')}"
        logger.info(f"中间切片 is_final=False 检验全部通过！")

    # 验证 MP4 文件与 ffprobe
    for idx, ev in enumerate(seg_finished_events, start=1):
        mp4_path = Path(ev["file_path"])
        assert mp4_path.exists(), f"切片文件不存在: {mp4_path}"
        info = probe_mp4(ffprobe_exe, mp4_path)
        logger.info(f"【切片 #{idx} ffprobe 检验】")
        logger.info(f"  文件: {info['file_name']}")
        logger.info(f"  时长: {info['duration']:.2f}s | 大小: {info['size_mb']}MB")
        logger.info(f"  分辨率: {info['width']}x{info['height']}")
        logger.info(f"  视频轨: {info['video_codec']} | 音频轨: {info['audio_codec']}")
        assert info["has_video"], f"切片 #{idx} 缺失视频轨道！"
        assert info["has_audio"], f"切片 #{idx} 缺失音频轨道！"
        assert info["duration"] > 0, f"切片 #{idx} 时长无效！"

        # 截取预览帧存到 artifacts
        out_jpg = preview_dir / f"e2e_live_verify_seg{idx:02d}.jpg"
        extract_preview_frame(ffmpeg_exe, mp4_path, out_jpg, time_sec=min(5, int(info["duration"] / 2)))
        # 同步复制到根目录方便直接渲染
        root_jpg = run_dir / f"seg{idx:02d}_05s.jpg"
        extract_preview_frame(ffmpeg_exe, mp4_path, root_jpg, time_sec=min(5, int(info["duration"] / 2)))

    logger.info("=======================================================")
    logger.info("🎉 模拟器实机端到端测试 100% 验收通过！")
    logger.info("=======================================================")


if __name__ == "__main__":
    from src.mvp.room_resolver import RoomResolver, RoomStatus
    resolver = RoomResolver(timeout=4)
    # 优先动态探测真实在播主播
    candidates = ["852703780", "60829777", "TuiTui.888_", "82708528984"]
    selected_target = None
    for cand in candidates:
        try:
            r = resolver.resolve(cand)
            if r.status == RoomStatus.LIVE and r.room_id:
                selected_target = r.room_id
                logger.info(f"探测到当前在播房间: {r.nickname} (web_rid: {cand}, room_id: {selected_target})")
                break
        except Exception as e:
            logger.warning(f"探测候选房间 {cand} 异常: {e}")

    if not selected_target:
        selected_target = "7704666294802123554"  # 林思琪默认 room_id
        logger.info(f"使用兜底 room_id: {selected_target}")

    run_e2e_test(
        vmindex=2,
        target=selected_target,
        segment_duration=40,
        record_seconds=65,
        window_mode="bottom",
    )
