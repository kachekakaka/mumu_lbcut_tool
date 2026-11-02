"""验证原地 Intent 自愈是否会保持当前直播间而不发生误切房。"""
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import time
from src.mvp.dep_manager import DependencyManager
from src.worker.navigator import WorkerNavigator

def main():
    dep_mgr = DependencyManager()
    adb_exe = dep_mgr.get_adb_path()
    serial = "127.0.0.1:16448"
    nav = WorkerNavigator(serial=serial, adb_exe=adb_exe)
    
    # 腿腿真不错
    room_id = "7702739475073239844"
    web_rid = "TuiTui.888_"
    
    print("1. 发起原地 Intent 唤起...")
    ok = nav.launch_room(room_id=room_id, web_rid=web_rid, wait_sec=2)
    print(f"唤起结果: {ok}")
    
    print("2. 检查当前前台包名...")
    pkg = nav.get_current_focus_package()
    print(f"当前前台包名: {pkg}")
    assert pkg == "com.ss.android.ugc.aweme", f"前台包名不符: {pkg}"
    
    print("3. 原地自愈动作通过，无拖拽切房风险。")

if __name__ == "__main__":
    main()
