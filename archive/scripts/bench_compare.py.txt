import time
import subprocess
import json
from pathlib import Path
from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient
from src.worker.navigator import WorkerNavigator

dep_mgr = DependencyManager()
adb_exe = dep_mgr.get_adb_path()
scrcpy_exe = dep_mgr.ensure_scrcpy()
ffprobe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.exe")

mumu = MuMuClient()
if not mumu.is_android_started(2):
    print("正在拉起 MuMu 实例 2...")
    mumu.launch_player(2, timeout_sec=60)
mumu.show_player_window(2)
serial = mumu.connect_adb(2)
if not serial:
    print("ADB 连接失败")
    exit(1)

out_dir = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\bench_compare")
out_dir.mkdir(parents=True, exist_ok=True)

nav = WorkerNavigator(serial=serial, adb_exe=adb_exe)

def test_recording(name: str, dur_sec: int = 10):
    mp4_path = out_dir / f"{name}.mp4"
    if mp4_path.exists():
        mp4_path.unlink()

    cmd = [
        str(scrcpy_exe),
        "-s", serial,
        f"--record={mp4_path}",
        "--no-playback",
        "--audio-source=playback",
        "--audio-codec=aac",
        "--video-codec=h264",
        "--require-audio",
        "--display-id=0",
        "--max-fps=30",
        "--video-bit-rate=4M",
        "--max-size=1280",
    ]
    env = dict(PYTHONPATH=".", ADB=str(adb_exe))
    print(f"\n>>> 开始测试 [{name}] 录制 {dur_sec}s ...")
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(dur_sec)
    import signal, os
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)
    proc.wait(timeout=5)
    time.sleep(1)

    # 分析
    probe_cmd = [str(ffprobe), "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(mp4_path)]
    res = subprocess.run(probe_cmd, capture_output=True, text=True)
    d = json.loads(res.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    fmt = d["format"]
    dur = float(fmt.get("duration", 0))
    nb = int(v.get("nb_frames", 0))
    bitrate = int(fmt.get("bit_rate", 0)) // 1000
    fps = nb / dur if dur > 0 else 0

    # 帧间隔分析
    cmd_f = [str(ffprobe), "-v", "quiet", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(mp4_path)]
    res_f = subprocess.run(cmd_f, capture_output=True, text=True)
    pts = [float(x.strip()) for x in res_f.stdout.strip().splitlines() if x.strip()]
    deltas = [pts[i] - pts[i-1] for i in range(1, len(pts))] if len(pts) > 1 else [0]
    max_d_ms = max(deltas) * 1000
    stutter_100 = sum(1 for x in deltas if x > 0.1)

    print(f"[{name}] 结果:")
    print(f"  时长: {dur:.2f}s | 帧数: {nb} | 真实FPS: {fps:.2f} | 码率: {bitrate} kbps")
    print(f"  最大帧间隔: {max_d_ms:.1f}ms | >100ms卡顿数: {stutter_100}")
    return fps

# 1. 当前抖音直播间 (东方甄选)
test_recording("1_dongfang_static", dur_sec=10)

# 2. 模拟手势滑动产生连续画面刷新 (高动态)
print("\n>>> 开始在直播间持续微幅上下滑动屏幕产生连续渲染...")
import threading
stop_swipe = False
def _swipe_loop():
    while not stop_swipe:
        nav.adb_client.shell("input swipe 500 1000 500 950 100")
        time.sleep(0.08)

t = threading.Thread(target=_swipe_loop)
t.start()
try:
    test_recording("2_dynamic_swiping", dur_sec=10)
finally:
    stop_swipe = True
    t.join()
