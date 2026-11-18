"""单路端到端录制全流程验证脚本 (MVP 验收)。"""
import argparse
import atexit
import logging
import sys
import time
from pathlib import Path

# 添加仓库根目录到 sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.mvp.config import MVP_OUTPUT_DIR
from src.mvp.dep_manager import DependencyManager
from src.mvp.mumu_client import MuMuClient
from src.mvp.room_resolver import RoomResolver, RoomStatus
from src.mvp.app_launcher import AppLauncher
from src.mvp.stream_recorder import StreamRecorder

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SingleVerify")


def run_single_verify(vmindex: int, target_url: str, duration_sec: int = 30) -> bool:
    logger.info("==================================================")
    logger.info(f"开始执行单路 MVP 验证: 实例 vmindex={vmindex}, 时长={duration_sec}s")
    logger.info(f"目标直播间: {target_url}")
    logger.info("==================================================")

    # 1. 确保外部依赖就绪
    dep_mgr = DependencyManager()
    scrcpy_exe = dep_mgr.ensure_scrcpy()
    adb_exe = dep_mgr.get_adb_path()
    logger.info(f"依赖自检完成: scrcpy={scrcpy_exe}, adb={adb_exe}")

    # 2. 解析直播间与开播状态
    resolver = RoomResolver()
    resolved = resolver.resolve(target_url)
    logger.info(f"直播间解析结果: web_rid={resolved.web_rid}, status={resolved.status.value}, title={resolved.title}, room_id={resolved.room_id}")

    # 3. 启动并连接 MuMu 模拟器
    mumu = MuMuClient(adb_exe=adb_exe)
    if not mumu.launch_player(vmindex):
        logger.error(f"启动 MuMu 实例 {vmindex} 失败！")
        return False

    serial = mumu.connect_adb(vmindex)
    if not serial:
        logger.error("未获取到有效的 ADB 设备连接！")
        return False
    logger.info(f"ADB 设备已连接: {serial}")

    # 4. 唤起抖音进入直播间
    launcher = AppLauncher(serial=serial, adb_exe=adb_exe)
    target_id = resolved.room_id or resolved.web_rid
    launcher.launch_live_room(room_id=target_id, web_rid=resolved.web_rid, wait_sec=4)

    # 5. 抓取截图留存证据
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    screenshot_path = MVP_OUTPUT_DIR / f"single_{vmindex}_{timestamp}.png"
    if launcher.capture_screenshot(screenshot_path):
        logger.info(f"已保存直播间页面截图证据: {screenshot_path}")

    # 6. 启动数字流录制管线
    output_video = MVP_OUTPUT_DIR / f"single_record_{vmindex}_{timestamp}.mp4"
    recorder = StreamRecorder(
        serial=serial,
        output_path=output_video,
        scrcpy_exe=scrcpy_exe,
        adb_exe=adb_exe,
    )

    # 注册退出清理钩子
    atexit.register(recorder.teardown)

    if not recorder.start():
        logger.error("录制管线启动失败！")
        return False

    # 自动放行可能出现的系统录音弹窗
    time.sleep(1)
    launcher.auto_dismiss_capture_dialog()

    logger.info(f"正在录制中，持续 {duration_sec} 秒...")
    step = 5
    elapsed = 0
    while elapsed < duration_sec:
        time.sleep(step)
        elapsed += step
        logger.info(f"录制进度: {elapsed}/{duration_sec}s, 当前文件大小: {recorder.get_file_size_mb():.2f}MB")

    # 7. 平稳停止并完成封装
    success = recorder.stop(timeout_sec=10)
    if not success or not output_video.exists():
        logger.error("录制未生成有效视频文件！")
        return False

    file_size_mb = output_video.stat().st_size / (1024 * 1024)
    logger.info(f"录制完成！视频路径: {output_video}, 最终大小: {file_size_mb:.2f}MB")
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="单路 MVP 验证脚本")
    parser.add_argument("--vmindex", type=int, default=2, help="MuMu 实例索引 (默认 2: lb1)")
    parser.add_argument("--url", type=str, default="https://live.douyin.com/168465454955", help="目标直播间链接或 web_rid")
    parser.add_argument("--duration", type=int, default=30, help="录制时长(秒)，默认 30")
    args = parser.parse_args()

    ok = run_single_verify(args.vmindex, args.url, args.duration)
    sys.exit(0 if ok else 1)
