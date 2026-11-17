"""调度 CLI：run 启动统一服务，其余入口只访问 HTTP。"""
import argparse
import json
import logging
from pathlib import Path
import uuid
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT.parent / "mumu_lbcut_tool_dev" / "runtime-console"


def build_parser():
    parser = argparse.ArgumentParser(description="MuMu 调度控制台")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="新增监控目标；重复地址返回既有 ID")
    add.add_argument("target", nargs="+")
    add.add_argument("--manual-only", action="store_true")
    add.add_argument("--allow-unconfirmed-identity", action="store_true",
                     help="显式接受身份未确认风险，不能放行确诊错房")
    status = sub.add_parser("status", help="通过 HTTP 查看分页状态或命令结果")
    status.add_argument("--command-id")
    status.add_argument("--limit", type=int, default=50)
    status.add_argument("--offset", type=int, default=0)
    status.add_argument("--history", action="store_true")
    controls = [add]
    for name, key in (("record", "target_id"), ("remove", "target_id")):
        p = sub.add_parser(name)
        p.add_argument(key)
        controls.append(p)
    stop = sub.add_parser("stop")
    stop.add_argument("job_id", nargs="?")
    stop.add_argument("--target-id")
    stop.add_argument("--session-key")
    controls.append(stop)
    rec = sub.add_parser("reconcile", help="请求对账；不会接管未知进程")
    rec.add_argument("job_id", nargs="?", default="")
    controls.append(rec)
    for client in [status, *controls]:
        client.add_argument("--server", default="http://127.0.0.1:8000")
    for client in controls:
        client.add_argument("--request-id", help="同一次操作重试必须使用原 UUID")
    run = sub.add_parser("run", help="运行调度；Ctrl+C 平滑停止本主控任务")
    run.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--open-browser", action="store_true",
                     help="服务就绪后在运行服务的计算机上打开默认浏览器")
    run.add_argument("--instances", default=None, help="可选初始启用的实例，例如 2,3；不传则保留数据库配置")
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--navigation-timeout", type=float, default=300.0,
                     help="传给Worker的定位总时限（60～900秒），默认300秒")
    run.add_argument("--scan-interval", type=float, default=30.0)
    run.add_argument("--segment-duration", type=int, default=1800)
    run.add_argument("--max-runtime", type=float, default=0,
                     help="验证用主控运行时限（秒），0 为持续运行")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    request_id = None
    try:
        if args.command == "run":
            from src.orchestrator.server import ServerConfig, run_server
            instances = ([int(v) for v in args.instances.split(",")] if args.instances.strip() else []) if args.instances is not None else None
            config = ServerConfig(args.data_dir.resolve(), args.output_dir.resolve(), instances,
                host=args.host, port=args.port, scan_interval=args.scan_interval, segment_duration=args.segment_duration,
                navigation_timeout=args.navigation_timeout, max_runtime=args.max_runtime,
                open_browser=args.open_browser)
            return run_server(config)
        if args.command == "stop" and (bool(args.job_id) == bool(args.target_id) or bool(args.target_id) != bool(args.session_key)):
            parser.error("stop JOB_ID 与 stop --target-id ID --session-key KEY 必须二选一")
        def call(method, path, **kw):
            response = requests.request(method, args.server.rstrip("/") + "/api/" + path, timeout=10, **kw)
            data = response.json()
            if not response.ok:
                raise RuntimeError(json.dumps(data, ensure_ascii=False))
            return data
        if args.command == "status":
            if args.command_id:
                result = call("GET", "commands/" + args.command_id)
            else:
                params = dict(limit=args.limit, offset=args.offset)
                result = dict(system=call("GET", "system"), overview=call("GET", "overview", params=params))
                for name in ("instances", "targets", "jobs", "segments"):
                    filters = dict(params, active="false" if args.history else "true") if name == "jobs" else params
                    result[name] = call("GET", name, params=filters)
        else:
            request_id = str(uuid.UUID(args.request_id)) if args.request_id else str(uuid.uuid4())
            method, body = "POST", {}
            if args.command == "add":
                path = "targets"
                body = dict(targets=args.target, manual_only=args.manual_only, allow_unconfirmed_identity=args.allow_unconfirmed_identity)
            elif args.command == "remove":
                method, path = "DELETE", "targets/" + args.target_id
            elif args.command == "record":
                path = "targets/" + args.target_id + "/record"
            elif args.command == "stop":
                path = "targets/" + args.target_id + "/stop" if args.target_id else "jobs/" + args.job_id + "/stop"
                body = dict(session_key=args.session_key) if args.target_id else {}
            else:
                path, body = "reconcile", dict(job_id=args.job_id or None)
            result = call(method, path, json=body, headers={"Idempotency-Key": request_id})
            result["request_id"] = request_id
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except requests.RequestException:
        print(json.dumps(dict(code="connection_failed", message="无法确认服务端结果；保留原请求编号，恢复连接后用相同参数和编号重试", request_id=request_id), ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps(dict(code="command_failed", message=str(exc), request_id=request_id), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
