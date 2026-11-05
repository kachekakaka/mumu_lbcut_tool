import json
import logging
from pathlib import Path
import subprocess
import time

from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient
from src.worker.navigator import WorkerNavigator

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("VerifyMuteAndWindow")

dep_mgr = DependencyManager()
adb_exe = dep_mgr.get_adb_path()
scrcpy_exe = dep_mgr.ensure_scrcpy()
ffprobe_exe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE")
ffmpeg_exe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffmpeg.EXE")

client = MuMuClient(adb_exe=adb_exe)
vmindex = 2

# 1. 确保实例启动
logger.info("检查并启动实例 2...")
if not client.is_android_started(vmindex):
    client.launch_player(vmindex)

# 2. 调出前台窗口展示给用户！
logger.info("调用 show_window 调出 MuMu 前台窗口...")
res_win = client._run_manager("control", "-v", str(vmindex), "show_window")
logger.info(f"show_window 结果: {res_win}")

# 3. 将模拟器声卡输出静音 (避免电脑扬声器嘈杂)
logger.info("设置 system_volume_close = true (拦截外放声音)...")
res_mute = client._run_manager("setting", "-v", str(vmindex), "-k", "system_volume_close", "-val", "true")
logger.info(f"setting 结果: {res_mute}")

# 4. 连接 ADB 并确保直播间在播
serial = client.connect_adb(vmindex)
logger.info(f"Connected to {serial}")

nav = WorkerNavigator(serial=serial, adb_exe=adb_exe)
room_id = "7704073922697906979"
web_rid = "dongfangzhenxuan"
logger.info("唤起东方甄选直播间...")
nav.launch_room(room_id=room_id, web_rid=web_rid, wait_sec=4)
nav.dismiss_system_dialogs()

from src.worker.pipeline import WorkerPipeline

# 5. 使用 WorkerPipeline 录制 8 秒 (自带 CTRL_BREAK_EVENT 优雅封口)
pipeline = WorkerPipeline(
    serial=serial,
    output_dir=Path("scripts"),
    scrcpy_exe=scrcpy_exe,
    adb_exe=adb_exe,
    port="27280:27290"
)
logger.info("启动 WorkerPipeline 录制 8 秒...")
out_mp4 = pipeline.start_segment(1)
time.sleep(8)
pipeline.stop_segment()
logger.info(f"WorkerPipeline 录制完成: {out_mp4}")

# 6. ffprobe 严苛核验
assert out_mp4.exists(), "产物 mp4 未生成！"
probe_cmd = [
    str(ffprobe_exe),
    "-v", "quiet",
    "-print_format", "json",
    "-show_format",
    "-show_streams",
    str(out_mp4),
]
res_probe = subprocess.run(probe_cmd, capture_output=True, text=True)
data = json.loads(res_probe.stdout)

video_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
audio_streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
dur = float(data.get("format", {}).get("duration", 0.0))
size_mb = int(data.get("format", {}).get("size", 0)) / (1024 * 1024)

logger.info(f"产物核验结果:")
logger.info(f"  文件大小: {size_mb:.2f} MB")
logger.info(f"  录制时长: {dur:.2f} s")
logger.info(f"  视频轨: {[s.get('codec_name') for s in video_streams]}")
logger.info(f"  音频轨: {[s.get('codec_name') for s in audio_streams]}")

# 7. 检测音频是否包含实际波形（非纯静音）
vol_cmd = [
    str(ffmpeg_exe),
    "-i", str(out_mp4),
    "-filter:a", "volumedetect",
    "-f", "null",
    "-",
]
res_vol = subprocess.run(vol_cmd, capture_output=True, text=True)
vol_lines = [line.strip() for line in res_vol.stderr.splitlines() if "mean_volume" in line or "max_volume" in line]
logger.info(f"  音频电平检测: {vol_lines}")

has_actual_audio = any("max_volume" in l and "-91.0 dB" not in l for l in vol_lines)
logger.info(f"  实际音频存在且非空: {has_actual_audio}")
logger.info("=== 测试完成 ===")
