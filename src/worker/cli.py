"""录播 Worker 独立子进程 CLI 入口。"""
import argparse
import atexit
import logging
from pathlib import Path
import signal
import sys

from src.worker.controller import WorkerConfig, WorkerController

# 日志输出至 stderr，确保 stdout 仅有纯净的 NDJSON 事件流
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] (Worker) %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("WorkerCLI")


def main():
    parser = argparse.ArgumentParser(description="抖音独立录播 Worker (执行单元)")
    parser.add_argument("--vmindex", type=int, required=True, help="绑定的 MuMu 模拟器实例索引 (如 2)")
    parser.add_argument("--target", type=str, required=True, help="直播间标识 (分享口令/短链/网页URL/web_rid)")
    parser.add_argument("--output-dir", type=str, required=True, help="切片录像保存目录")
    parser.add_argument("--segment-duration", type=int, default=1800, help="切片时长(秒)，默认 1800 (30分钟)")
    parser.add_argument("--total-duration", type=int, default=0, help="总录制限制时长(秒)，0 表示持续录制")
    parser.add_argument("--stall-timeout", type=float, default=10.0, help="卡顿自愈检测阈值(秒)，默认 10")
    parser.add_argument("--heartbeat-interval", type=float, default=3.0, help="心跳间隔(秒)，默认 3")

    args = parser.parse_args()

    config = WorkerConfig(
        vmindex=args.vmindex,
        target=args.target,
        output_dir=Path(args.output_dir),
        segment_duration_sec=args.segment_duration,
        total_duration_sec=args.total_duration,
        stall_timeout_sec=args.stall_timeout,
        heartbeat_interval_sec=args.heartbeat_interval,
    )

    controller = WorkerController(config=config)

    # 绑定信号处理器
    def _signal_handler(signum, frame):
        logger.info(f"Worker 收到系统中断信号 ({signum})，正在请求平稳封口退出...")
        controller.request_stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    exit_code = controller.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
