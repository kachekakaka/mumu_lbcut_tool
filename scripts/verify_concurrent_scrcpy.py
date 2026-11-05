"""验证在 MuMu 模拟器环境下两个 scrcpy 实例是否支持并发录制（音视频双轨验证）。"""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from src.mvp.config import SCRCPY_DIR, get_adb_exe
from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("ConcurrentProbe")

def probe_file(ffprobe_exe: Path, file_path: Path):
    cmd = [
        str(ffprobe_exe),
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    data = json.loads(res.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))
    has_video = any(s.get("codec_type") == "video" for s in data.get("streams", []))
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return has_video, has_audio, duration

def main():
    vmindex = 2
    out_dir = Path(r"..\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\verify_concurrent")
    out_dir.mkdir(parents=True, exist_ok=True)
    file_a = out_dir / "probe_pipe_a.mp4"
    file_b = out_dir / "probe_pipe_b.mp4"

    # 清理旧测试产物
    if file_a.exists(): file_a.unlink()
    if file_b.exists(): file_b.unlink()

    dep_mgr = DependencyManager()
    scrcpy_exe = dep_mgr.ensure_scrcpy()
    adb_exe = dep_mgr.get_adb_path()
    ffprobe_exe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE")

    client = MuMuClient(adb_exe=adb_exe)
    logger.info(f"检查并拉起 MuMu 实例 {vmindex}...")
    if not client.launch_player(vmindex):
        logger.error("MuMu 实例拉起失败")
        sys.exit(1)

    serial = client.connect_adb(vmindex)
    if not serial:
        logger.error("ADB 连接失败")
        sys.exit(1)

    logger.info(f"已就绪模拟器 ADB serial: {serial}")

    env = os.environ.copy()
    if adb_exe and adb_exe.exists():
        env["ADB"] = str(adb_exe)
        env["PATH"] = str(adb_exe.parent) + os.path.pathsep + env.get("PATH", "")

    # 1. 启动 Pipe A (端口 27183:27190)
    cmd_a = [
        str(scrcpy_exe),
        "-s", serial,
        f"--record={file_a}",
        "--no-playback",
        "--audio-source=playback",
        "--audio-codec=aac",
        "--video-codec=h264",
        "--port=27183:27190",
        "--max-fps=30",
        "--max-size=1280",
    ]
    logger.info("启动 Pipe A 录制进程...")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc_a = subprocess.Popen(cmd_a, env=env, creationflags=flags)

    time.sleep(3.0)  # 让 Pipe A 运行 3 秒

    # 2. 并发启动 Pipe B (端口 27191:27200)
    cmd_b = [
        str(scrcpy_exe),
        "-s", serial,
        f"--record={file_b}",
        "--no-playback",
        "--audio-source=playback",
        "--audio-codec=aac",
        "--video-codec=h264",
        "--port=27191:27200",
        "--max-fps=30",
        "--max-size=1280",
    ]
    logger.info("启动 Pipe B 录制进程（进入交叠并行期）...")
    proc_b = subprocess.Popen(cmd_b, env=env, creationflags=flags)

    # 3. 两者共同录制 5 秒交叠
    logger.info("正在执行 5 秒双管线并发交叠录制...")
    time.sleep(5.0)

    # 4. 关停 Pipe A
    logger.info("关停 Pipe A 录制进程...")
    if os.name == "nt":
        import signal
        proc_a.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc_a.terminate()
    proc_a.wait(timeout=5.0)

    # 5. Pipe B 继续单录 3 秒
    time.sleep(3.0)

    # 6. 关停 Pipe B
    logger.info("关停 Pipe B 录制进程...")
    if os.name == "nt":
        import signal
        proc_b.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc_b.terminate()
    proc_b.wait(timeout=5.0)

    logger.info("双管线录制已结束，正在分析两份产物音视频完整性...")
    v_a, a_a, d_a = probe_file(ffprobe_exe, file_a)
    v_b, a_b, d_b = probe_file(ffprobe_exe, file_b)

    size_a_mb = file_a.stat().st_size / (1024 * 1024)
    size_b_mb = file_b.stat().st_size / (1024 * 1024)

    print("\n" + "="*50)
    print("【实机双管线并发实测报告】")
    print(f"Pipe A: 存在={file_a.exists()}, 大小={size_a_mb:.2f}MB, 时长={d_a:.2f}s, 视频轨={v_a}, 音频轨={a_a}")
    print(f"Pipe B: 存在={file_b.exists()}, 大小={size_b_mb:.2f}MB, 时长={d_b:.2f}s, 视频轨={v_b}, 音频轨={a_b}")
    print("="*50 + "\n")

    if v_a and a_a and v_b and a_b:
        print(">>> 结论：MuMu 实例完全支持 scrcpy 双管线并发交叠录制！音视频双轨均完好无损！<<<")
    else:
        print(">>> 结论：不支持并发录制，存在轨道丢失或冲突！<<<")

if __name__ == "__main__":
    main()
