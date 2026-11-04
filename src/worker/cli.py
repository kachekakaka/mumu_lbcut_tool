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


def build_arg_parser() -> argparse.ArgumentParser:
    """构建 Worker 命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="抖音独立录播 Worker (执行单元)")
    parser.add_argument("--vmindex", type=int, required=True, help="绑定的 MuMu 模拟器实例索引 (如 2)")
    parser.add_argument("--target", type=str, required=True, help="直播间标识 (分享口令/短链/网页URL/web_rid)")
    parser.add_argument("--output-dir", type=str, required=True, help="切片录像保存目录")
    parser.add_argument(
        "--segment-duration", "--segment",
        dest="segment_duration",
        type=int,
        default=1800,
        help="切片时长(秒)，默认 1800 (30分钟)"
    )
    parser.add_argument(
        "--total-duration", "--duration",
        dest="total_duration",
        type=int,
        default=0,
        help="总录制限制时长(秒)，0 表示持续录制"
    )
    parser.add_argument("--stall-timeout", type=float, default=10.0, help="卡顿自愈检测阈值(秒)，默认 10")
    parser.add_argument("--heartbeat-interval", type=float, default=3.0, help="心跳间隔(秒)，默认 3")
    parser.add_argument(
        "--allow-unconfirmed-identity",
        action="store_true",
        default=False,
        help="逃逸开关：当主播身份处于无法确认状态时允许临时录制（确诊错房仍强制阻断）"
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """对解析后的命令行参数进行前置强校验。

    若参数非法，输出错误提示至 stderr 并以退出码 2 终止。
    """
    errors = []

    if args.vmindex < 0:
        errors.append(f"--vmindex 必须是非负整数，当前为 {args.vmindex}")

    if not args.target or not args.target.strip():
        errors.append("--target 直播间标识不能为空或空白字符")

    if args.segment_duration <= 0:
        errors.append(f"--segment-duration 必须为正整数（大于 0），当前为 {args.segment_duration}")

    if args.total_duration < 0:
        errors.append(f"--total-duration 不能为负数，当前为 {args.total_duration}")

    if args.total_duration > 0 and args.segment_duration > args.total_duration:
        errors.append(
            f"切片时长 --segment-duration ({args.segment_duration}s) 不能大于总录制时长 --total-duration ({args.total_duration}s)"
        )

    if args.stall_timeout <= 0:
        errors.append(f"--stall-timeout 必须为正数，当前为 {args.stall_timeout}")

    if args.heartbeat_interval <= 0:
        errors.append(f"--heartbeat-interval 必须为正数，当前为 {args.heartbeat_interval}")

    if errors:
        for err in errors:
            sys.stderr.write(f"[参数错误] {err}\n")
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    validate_args(args)

    config = WorkerConfig(
        vmindex=args.vmindex,
        target=args.target,
        output_dir=Path(args.output_dir),
        segment_duration_sec=args.segment_duration,
        total_duration_sec=args.total_duration,
        stall_timeout_sec=args.stall_timeout,
        heartbeat_interval_sec=args.heartbeat_interval,
        allow_unconfirmed_identity=args.allow_unconfirmed_identity,
    )

    controller = WorkerController(config=config)

    # 绑定信号处理器
    def _signal_handler(signum, frame):
        logger.info(f"Worker 收到系统中断信号 ({signum})，正在请求平稳封口退出...")
        controller.request_stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    exit_code = controller.run()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
