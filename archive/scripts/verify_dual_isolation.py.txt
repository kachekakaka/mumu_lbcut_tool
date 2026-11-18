"""双路并发音频彻底隔离验证脚本 (MVP 验收 AC-5)。"""
import argparse
import atexit
import logging
import sys
import time
import subprocess
from pathlib import Path

# 添加仓库根目录到 sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.mvp.config import MVP_OUTPUT_DIR
from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient
from src.mvp.room_resolver import RoomResolver
from src.mvp.app_launcher import AppLauncher
from src.mvp.stream_recorder import StreamRecorder

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("DualVerify")


def run_dual_verify(
    vm1: int, url1: str,
    vm2: int, url2: str,
    duration_sec: int = 30
) -> bool:
    logger.info("==================================================")
    logger.info(f"开始执行双路并发与音频隔离 MVP 验证")
    logger.info(f"路 A: 实例 vmindex={vm1}, 直播间={url1}")
    logger.info(f"路 B: 实例 vmindex={vm2}, 直播间={url2}")
    logger.info(f"并发录制时长: {duration_sec}s")
    logger.info("==================================================")

    # 1. 确保外部依赖就绪
    dep_mgr = DependencyManager()
    scrcpy_exe = dep_mgr.ensure_scrcpy()
    adb_exe = dep_mgr.get_adb_path()

    # 2. 直播间解析
    resolver = RoomResolver()
    res1 = resolver.resolve(url1)
    res2 = resolver.resolve(url2)
    logger.info(f"路 A 解析: {res1.web_rid} ({res1.status.value})")
    logger.info(f"路 B 解析: {res2.web_rid} ({res2.status.value})")

    # 3. 启动两个模拟器
    mumu = MuMuClient(adb_exe=adb_exe)
    logger.info(f"正在并发启动模拟器实例 {vm1} 与 {vm2}...")
    mumu.launch_player(vm1)
    mumu.launch_player(vm2)

    # 4. 获取 ADB 连接
    serial1 = mumu.connect_adb(vm1)
    serial2 = mumu.connect_adb(vm2)
    logger.info(f"路 A 设备: {serial1}")
    logger.info(f"路 B 设备: {serial2}")
    if not serial1 or not serial2 or serial1 == serial2:
        logger.error(f"未能获取两个不同的有效 ADB 设备: s1={serial1}, s2={serial2}")
        return False

    # 5. 分别唤起各自直播间
    launcher1 = AppLauncher(serial=serial1, adb_exe=adb_exe)
    launcher2 = AppLauncher(serial=serial2, adb_exe=adb_exe)
    launcher1.launch_live_room(room_id=res1.room_id or res1.web_rid, web_rid=res1.web_rid, wait_sec=3)
    launcher2.launch_live_room(room_id=res2.room_id or res2.web_rid, web_rid=res2.web_rid, wait_sec=3)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out1 = MVP_OUTPUT_DIR / f"dual_rec_A_{vm1}_{timestamp}.mp4"
    out2 = MVP_OUTPUT_DIR / f"dual_rec_B_{vm2}_{timestamp}.mp4"

    rec1 = StreamRecorder(serial=serial1, output_path=out1, scrcpy_exe=scrcpy_exe, adb_exe=adb_exe)
    rec2 = StreamRecorder(serial=serial2, output_path=out2, scrcpy_exe=scrcpy_exe, adb_exe=adb_exe)

    atexit.register(rec1.teardown)
    atexit.register(rec2.teardown)

    # 6. 同时开始录制
    logger.info("正在同时启动双路录制...")
    ok1 = rec1.start()
    ok2 = rec2.start()
    if not ok1 or not ok2:
        logger.error(f"双路启动失败: A={ok1}, B={ok2}")
        rec1.teardown()
        rec2.teardown()
        return False

    # 放行弹窗
    time.sleep(1)
    launcher1.auto_dismiss_capture_dialog()
    launcher2.auto_dismiss_capture_dialog()

    logger.info(f"双路录制进行中，持续 {duration_sec} 秒...")
    step = 5
    elapsed = 0
    while elapsed < duration_sec:
        time.sleep(step)
        elapsed += step
        logger.info(
            f"进度 {elapsed}/{duration_sec}s | "
            f"路 A: {rec1.get_file_size_mb():.2f}MB, 运行: {rec1.is_recording()} | "
            f"路 B: {rec2.get_file_size_mb():.2f}MB, 运行: {rec2.is_recording()}"
        )

    # 7. 同时停止录制
    logger.info("录制完成，正在平稳停止双路录制...")
    stop1 = rec1.stop(timeout_sec=10)
    stop2 = rec2.stop(timeout_sec=10)

    if not stop1 or not stop2 or not out1.exists() or not out2.exists():
        logger.error("录制未能成功生成两个视频文件！")
        return False

    logger.info("==================================================")
    logger.info(f"双路录制验证成功！")
    logger.info(f"路 A 视频: {out1} ({rec1.get_file_size_mb():.2f}MB)")
    logger.info(f"路 B 视频: {out2} ({rec2.get_file_size_mb():.2f}MB)")
    logger.info("==================================================")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="双路并发与音频隔离验证脚本")
    parser.add_argument("--vm1", type=int, default=2, help="实例 A 索引 (默认 2: lb1)")
    parser.add_argument("--url1", type=str, default="https://live.douyin.com/168465454955", help="直播间 A")
    parser.add_argument("--vm2", type=int, default=3, help="实例 B 索引 (默认 3: lb2)")
    parser.add_argument("--url2", type=str, default="https://live.douyin.com/rourou.004", help="直播间 B")
    parser.add_argument("--duration", type=int, default=30, help="录制时长(秒)")
    args = parser.parse_args()

    ok = run_dual_verify(args.vm1, args.url1, args.vm2, args.url2, args.duration)
    sys.exit(0 if ok else 1)
