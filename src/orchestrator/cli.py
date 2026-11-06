"""最小调度 CLI：SQLite 命令队列与单进程运行入口，不包含 WebUI/上传服务。"""
import argparse
import json
import logging
from pathlib import Path
import signal
import threading
import time

from src.orchestrator.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT.parent / "mumu_lbcut_tool_dev" / "runtime"


def build_parser():
    parser = argparse.ArgumentParser(description="MuMu 自动录播最小调度器")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="长期运行数据目录；不放在临时产物目录")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="新增监控目标；重复地址返回既有 ID")
    add.add_argument("target")
    add.add_argument("--manual-only", action="store_true")
    add.add_argument("--allow-unconfirmed-identity", action="store_true",
                     help="显式接受身份未确认风险，不能放行确诊错房")
    sub.add_parser("status", help="查看实例、任务、目标和切片")
    for name, key in (("record", "target_id"), ("remove", "target_id"),
                      ("stop", "job_id"), ("unblock", "vmindex")):
        p = sub.add_parser(name)
        p.add_argument(key)
    rec = sub.add_parser("reconcile", help="请求对账；不会接管未知进程")
    rec.add_argument("job_id", nargs="?", default="")
    run = sub.add_parser("run", help="运行调度；Ctrl+C 平滑停止本主控任务")
    run.add_argument("--instances", required=True, help="明确允许使用的实例，例如 2,3")
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--scan-interval", type=float, default=30.0)
    run.add_argument("--segment-duration", type=int, default=1800)
    run.add_argument("--overlap-duration", type=float, default=0.0)
    run.add_argument("--recording-mode", choices=["continuous", "restart"], default="continuous")
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--max-runtime", type=float, default=0,
                     help="验证用主控运行时限（秒），0 为持续运行")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "run":
        try:
            indices = [int(x) for x in args.instances.split(",")]
            if not indices or any(i < 0 for i in indices):
                raise ValueError()
            if args.scan_interval <= 0 or args.segment_duration <= 0 or args.max_attempts < 1:
                raise ValueError()
            if args.recording_mode == 'continuous' and args.overlap_duration:
                raise ValueError()
            if args.overlap_duration < 0 or args.overlap_duration >= args.segment_duration or args.max_runtime < 0:
                raise ValueError()
        except ValueError:
            parser.error("实例索引或时间/重试参数非法")
    store = Store(args.data_dir.resolve() / "orchestrator.sqlite3")
    try:
        if args.command == "add":
            key = store.add_target(args.target, auto_record=not args.manual_only,
                                   allow_unconfirmed=args.allow_unconfirmed_identity)
            print(json.dumps({"target_id": key}, ensure_ascii=False))
        elif args.command == "status":
            result = {name: store.rows(f"SELECT * FROM {name}") for name in
                      ("targets", "instances", "jobs", "segments", "commands")}
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command != "run":
            subject = getattr(args, "target_id", getattr(args, "job_id", getattr(args, "vmindex", "")))
            key = store.enqueue_command(args.command, str(subject))
            print(json.dumps({"command_id": key, "status": "queued", "note": "由运行中的调度器执行"}, ensure_ascii=False))
        else:
            from src.orchestrator.scheduler import Scheduler
            scheduler = Scheduler(store, args.output_dir, indices,
                scan_interval_sec=args.scan_interval, max_attempts=args.max_attempts,
                segment_duration_sec=args.segment_duration, overlap_duration_sec=args.overlap_duration,
                recording_mode=args.recording_mode)
            stop = threading.Event()
            def request_stop(signum, frame):
                stop.set()
            old = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, request_stop)
            if hasattr(signal, "SIGBREAK"):
                old[signal.SIGBREAK] = signal.signal(signal.SIGBREAK, request_stop)
            started = time.monotonic()
            try:
                while not stop.is_set():
                    scheduler.tick()
                    if args.max_runtime and time.monotonic() - started >= args.max_runtime:
                        break
                    stop.wait(.2)
            finally:
                scheduler.close()
                for sig, handler in old.items():
                    signal.signal(sig, handler)
        return 0
    except Exception:
        logging.exception("调度命令失败；数据库与录像保留供对账")
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
