"""录播 Worker 独立子进程 CLI 入口。"""
import argparse
import atexit
import logging
from pathlib import Path
import signal
import sys

from src.worker.controller import WorkerConfig, WorkerController
from src.worker.room_locator import validate_navigation_timeout, validate_expected_identity

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
    parser.add_argument("--target", type=str, required=True, help="默认传入独立查询得到的内部 room_id；网页入口仅用于显式未确认模式")
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
    parser.add_argument("--navigation-timeout", type=float, default=300.0,
                        help="定位、取证和画面就绪总时限（60～900秒），默认300秒；不含模拟器启动")
    parser.add_argument("--stall-timeout", type=float, default=10.0, help="卡顿自愈检测阈值(秒)，默认 10")
    parser.add_argument("--heartbeat-interval", type=float, default=3.0, help="心跳间隔(秒)，默认 3")
    parser.add_argument(
        "--allow-unconfirmed-identity",
        action="store_true",
        default=False,
        help="显式允许有界复核后身份未确认的录制；任何未解决的可靠冲突仍阻断"
    )
    parser.add_argument(
        "--background",
        action="store_true",
        default=False,
        help="后台静默录制别名（等同于 --window-mode hidden）"
    )
    parser.add_argument(
        "--window-mode",
        choices=["foreground", "bottom", "offscreen", "hidden"],
        default=None,
        help="模拟器窗口呈现模式：foreground(正常前台), bottom(放置在其他窗口底层且不抢焦点), offscreen(移出主工作区边缘), hidden(完全后台隐藏静默)"
    )
    parser.add_argument(
        "--window-pos-x",
        type=int,
        default=None,
        help="窗口 X 坐标（用于 offscreen 边缘模式或自定义摆放位置，负数或大坐标即移出屏幕）"
    )
    parser.add_argument(
        "--window-pos-y",
        type=int,
        default=None,
        help="窗口 Y 坐标（用于 offscreen 边缘模式或自定义摆放位置）"
    )
    parser.add_argument("--target-kind", choices=["auto", "room_id", "web_rid"], default="auto")
    parser.add_argument("--web-rid", default=None, help="内部场次 ID 的网页入口备用值")
    parser.add_argument("--expected-anchor-uid", default=None, help="独立解析的主播 UID，不是 room_id")
    parser.add_argument("--job-id", default=None, help="调度任务关联标识")
    parser.add_argument("--event-log", default=None,
                        help="事件日志路径：stdout 断开（调度主控已退出）后事件改为追加到此文件，并平滑封口退出")
    parser.add_argument("--log-file", default=None,
                        help="运行日志另存路径（追加）；stderr 照常输出")
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """对解析后的命令行参数进行前置强校验。

    若参数非法，输出错误提示至 stderr 并以退出码 2 终止。
    """
    errors = []
    try:
        validate_navigation_timeout(args.navigation_timeout)
        validate_expected_identity(args.target, args.target_kind, args.expected_anchor_uid,
                                   args.allow_unconfirmed_identity)
    except ValueError as exc:
        errors.append(str(exc))

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

    eff_window_mode = args.window_mode
    if eff_window_mode is None:
        eff_window_mode = "hidden" if args.background else "foreground"

    config = WorkerConfig(
        vmindex=args.vmindex,
        target=args.target,
        navigation_timeout_sec=args.navigation_timeout,
        target_kind=args.target_kind,
        web_rid=args.web_rid,
        expected_anchor_uid=args.expected_anchor_uid,
        job_id=args.job_id,
        output_dir=Path(args.output_dir),
        segment_duration_sec=args.segment_duration,
        total_duration_sec=args.total_duration,
        stall_timeout_sec=args.stall_timeout,
        heartbeat_interval_sec=args.heartbeat_interval,
        allow_unconfirmed_identity=args.allow_unconfirmed_identity,
        background_mode=(eff_window_mode == "hidden"),
        window_mode=eff_window_mode,
        window_pos_x=args.window_pos_x,
        window_pos_y=args.window_pos_y,
        event_log_path=Path(args.event_log) if args.event_log else None,
    )

    log_handler = None
    if args.log_file:
        # 由 Worker 自己写文件：主控被强杀后 stderr 管道失效，封口阶段的日志仍需保留。
        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        log_handler = logging.FileHandler(args.log_file, encoding="utf-8")
        log_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)s] (Worker) %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        logging.getLogger().addHandler(log_handler)

    controller = WorkerController(config=config)

    # 绑定信号处理器
    def _signal_handler(signum, frame):
        logger.info(f"Worker 收到系统中断信号 ({signum})，正在请求平稳封口退出...")
        controller.request_stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    try:
        return controller.run()
    finally:
        if log_handler:
            logging.getLogger().removeHandler(log_handler)
            log_handler.close()


if __name__ == "__main__":
    sys.exit(main())
