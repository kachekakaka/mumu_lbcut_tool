import time
import subprocess
import json
from pathlib import Path
from src.mvp.dep_manager import DependencyManager
from src.worker.pipeline import WorkerPipeline

dep_mgr = DependencyManager()
adb_exe = dep_mgr.get_adb_path()
scrcpy_exe = dep_mgr.ensure_scrcpy()
ffprobe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.exe")

serial = "127.0.0.1:16448"
out_dir = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\test_single_pipe").resolve()
out_dir.mkdir(parents=True, exist_ok=True)

pipe = WorkerPipeline(
    serial=serial,
    output_dir=out_dir,
    scrcpy_exe=scrcpy_exe,
    adb_exe=adb_exe,
    max_fps=30,
    bit_rate="4M",
    max_size=1280,
    port=27195,
)

print(f"启动单管线录制到 {out_dir} ...")
try:
    p = pipe.start_segment(1)
    print(f"开始录制中，目标文件: {p}")
    for i in range(10):
        time.sleep(1)
        print(f"[{i+1}s] recording: {pipe.is_recording()}, size={pipe.get_current_file_size_mb():.2f}MB, logs={pipe._last_logs[-2:]}")
        if not pipe.is_recording():
            print("录制异常中断！")
            break
    pipe.stop_segment()
    print(f"录制结束，文件存在: {p.exists()}，大小: {p.stat().st_size if p.exists() else 0}")
except Exception as e:
    print(f"异常: {e}")
    if pipe._proc:
        out, err = pipe._proc.communicate(timeout=2)
        print("stdout:", out)
        print("stderr:", err)
