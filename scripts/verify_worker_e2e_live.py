from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""指定目标的短时 Worker 验证；默认只预览，结果按事件和媒体分别判定。"""
import argparse
import json
import os
import signal
import subprocess
import threading
import time
from scripts._common import entry, nonnegative, positive, output_parent, preview, new_batch, write_json, media_tools, probe, media_summary
from scripts._device import cancellation

# 覆盖客户端 5、采集停止 15、尾片校验 24、探针回收 30、映射/应用收尾 10、调度余量 6 秒。
STOP_GRACE = 5 + 15 + 24 + 30 + 10 + 6

def unique_events(events):
    result, seen = [], {}
    for event in events:
        eid = event.get('event_id')
        if not eid:
            raise ValueError('事件缺少 event_id')
        if eid in seen:
            if seen[eid] != event:
                raise ValueError('相同事件 ID 的内容冲突')
            continue
        seen[eid] = event
        result.append(event)
    return result

def warning_event(e):
    return (e.get('event') in {'stall_detected','stall_healed','segment_quality_warning','login_blocked','overlay_recovery'}
            or e.get('event') == 'room_identity_check' and e.get('action') not in {'confirmed','sample','discarded'}
            or e.get('event') == 'capture_startup' and e.get('action') in {'rejected','discarded'}
            or e.get('event') == 'capture_session' and bool(e.get('reason')))

def ready_to_stop(events):
    finished = {e.get('segment_index') for e in events if e.get('event') in {'segment_finished','segment_aborted'}}
    return (any(e.get('event') == 'segment_finished' and e.get('is_final') is False for e in events)
            and any(e.get('event') == 'segment_started' and e.get('segment_index') not in finished for e in events)
            and not any(e.get('event') in {'worker_stopped','worker_failed','stream_ended'} for e in events)
            and not any(warning_event(e) for e in events))

def assess(events, batch, exit_code, planned_stop, forced, *, signal_time=None):
    reasons = []
    try:
        events = unique_events(events)
        starts = [e for e in events if e.get('event') == 'worker_started']
        stops = [e for e in events if e.get('event') == 'worker_stopped']
        if len(starts) != 1 or len(stops) != 1 or not starts[0].get('run_id'):
            raise ValueError('任务启动/终态不唯一或缺少 run_id')
        run_id = starts[0]['run_id']
        if any(e.get('run_id') != run_id for e in events):
            raise ValueError('事件混入其他运行')
        opened = [e for e in events if e.get('event') == 'segment_started']
        closed = [e for e in events if e.get('event') in {'segment_finished','segment_aborted'}]
        if len({e['segment_index'] for e in opened}) != len(opened) or len(opened) != len(closed):
            raise ValueError('切片启动与终态不配对')
        task_dir = batch.resolve() / run_id
        for start in opened:
            matching = [e for e in closed if e.get('segment_index') == start['segment_index']]
            if len(matching) != 1:
                raise ValueError('切片终态冲突')
            index = start['segment_index']
            source = Path(start['file_path']).resolve()
            path = Path(matching[0]['file_path']).resolve()
            # 当前连续采集协议：临时 part 文件封口后移到任务目录并改为正式名称。
            expected = task_dir / f"{starts[0]['target']}_{run_id}_{index:06d}.mp4"
            if (batch.resolve() not in task_dir.parents or source.parent.parent != task_dir
                    or not source.parent.name.startswith('capture_')
                    or source.name != f'part_{index:06d}.mp4' or path != expected):
                raise ValueError('切片文件映射不一致')
            if not path.is_file():
                raise ValueError('切片缺失或不属于本次批次')
        finished = [e for e in closed if e['event'] == 'segment_finished']
        finals = [e for e in finished if e.get('is_final') is True]
        if forced or exit_code != 0 or stops[0].get('exit_code') != 0:
            raise ValueError('进程异常退出或使用强制停止')
        if any(e.get('event') in {'worker_failed','segment_aborted'} for e in events):
            raise ValueError('发生任务失败或异常切片')
        if not planned_stop or stops[0].get('reason') != 'stop_requested' or any(e.get('event') == 'stream_ended' for e in events):
            return dict(status='验收条件未满足', reasons=['未完成计划内主动中断'], finished=finished)
        if len(finished) < 2 or len(finals) != 1 or finals[0] != finished[-1] or any(e.get('is_final') is not False for e in finished[:-1]):
            return dict(status='验收条件未满足', reasons=['缺少正常非最终片或唯一最终片'], finished=finished)
        if signal_time is None or finals[0].get('timestamp', 0) < signal_time:
            return dict(status='验收条件未满足', reasons=['最终片不是停止信号之后完成'], finished=finished)
        if stops[0].get('had_segment_errors') is not False or any(warning_event(e) for e in events):
            return dict(status='有恢复/质量告警', reasons=['发生过异常、恢复或质量风险'], finished=finished)
        return dict(status='正常通过', reasons=[], finished=finished)
    except (ValueError, KeyError, TypeError) as exc:
        reasons.append(str(exc))
        return dict(status='失败', reasons=reasons, finished=[])

def track_children(proc, owned, allowed):
    import psutil
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
    except psutil.Error:
        return
    for child in children:
        try:
            if Path(child.exe()).resolve() in allowed:
                owned[child.pid] = (child.create_time(), child)
        except (psutil.Error, OSError):
            continue

def force_owned(proc, owned):
    import psutil
    errors = []
    for pid, (created, child) in owned.items():
        try:
            if child.is_running() and child.create_time() == created:
                child.kill()
                child.wait(timeout=1)
        except psutil.NoSuchProcess:
            pass
        except psutil.Error as exc:
            errors.append(f'{pid}: {exc}')
    if proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            errors.append('Worker 未在回收期限内退出')
    return errors

def run_worker(args, batch, target, uid, web_rid, tools, scrcpy):
    from scripts._common import ROOT
    from src.mvp.mumu_client import LAUNCH_BUDGET_SEC, CONNECT_BUDGET_SEC, SETTINGS_BUDGET_SEC
    # 工具检查20、前后查询15、开录复核30与采集25、父进程余量15。
    startup_budget = 20 + 15 + 20 + LAUNCH_BUDGET_SEC + CONNECT_BUDGET_SEC + SETTINGS_BUDGET_SEC + args.navigation_timeout + 30 + 25 + 15
    cmd = [sys.executable, '-m', 'src.worker.cli', '--vmindex', str(args.vmindex),
           '--target', target, '--target-kind', args.target_kind, '--output-dir', str(batch),
           '--segment-duration', str(args.segment_duration), '--navigation-timeout', str(args.navigation_timeout),
           '--window-mode', 'bottom', '--require-local-tools', '--event-log', str(batch/'fallback.ndjson')]
    if uid: cmd += ['--expected-anchor-uid', uid]
    if web_rid: cmd += ['--web-rid', web_rid]
    if args.allow_unconfirmed_identity: cmd += ['--allow-unconfirmed-identity']
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED='1',
               SCRCPY_SERVER_PATH=str(scrcpy.parent/'scrcpy-server'))
    events, errors, owned = [], [], {}
    guard = threading.Lock()
    proc = None
    threads = []
    planned, forced, signal_time = False, False, None
    start = time.monotonic()
    allowed = {Path(p).resolve() for p in (scrcpy, tools.ffmpeg, tools.ffprobe)}
    raw = (batch/'events.ndjson').open('x', encoding='utf-8')
    logs = (batch/'worker.log').open('x', encoding='utf-8')
    def drain(stream, file, is_events):
        try:
            for line in stream:
                file.write(line); file.flush()
                if is_events:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict): raise ValueError('事件不是对象')
                        with guard: events.append(event)
                    except ValueError as exc:
                        with guard: errors.append(f'事件解析失败：{exc}')
        except Exception as exc:
            with guard: errors.append(f'日志读取/写入失败：{exc}')
        finally:
            stream.close()
    try:
        with cancellation() as stop:
            proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding='utf-8', errors='replace',
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=='nt' else 0)
            for stream, file, is_events in [(proc.stdout, raw, True), (proc.stderr, logs, False)]:
                thread = threading.Thread(target=drain, args=(stream,file,is_events), daemon=True)
                thread.start(); threads.append(thread)
            first = None
            try:
                while proc.poll() is None:
                    track_children(proc, owned, allowed)
                    with guard: snapshot, bad = list(events), list(errors)
                    now = time.monotonic()
                    if first is None and any(e.get('event')=='segment_started' for e in snapshot):
                        first = now
                    if stop.is_set() or bad: break
                    if first is None and now-start >= startup_budget:
                        errors.append('开录总预算耗尽'); break
                    if first is not None and now-first >= args.record_seconds:
                        planned = ready_to_stop(snapshot); break
                    stop.wait(.1)
            finally:
                if proc.poll() is None:
                    signal_time = time.time()
                    proc.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
                    until = time.monotonic()+STOP_GRACE
                    while proc.poll() is None and time.monotonic()<until:
                        track_children(proc, owned, allowed); time.sleep(.1)
                    if proc.poll() is None:
                        forced = True
                        errors.extend(force_owned(proc, owned))
                for thread in threads: thread.join(timeout=1)
                if any(thread.is_alive() for thread in threads):
                    errors.append('日志管道未在期限内关闭')
                remaining = []
                for pid, (created, child) in owned.items():
                    try:
                        if child.is_running() and child.create_time() == created:
                            remaining.append(pid)
                    except Exception:
                        continue
                if remaining:
                    forced = True
                    errors.append(f'Worker 退出后仍有本次采集/分析进程：{remaining}')
                    errors.extend(force_owned(proc, owned))
            if stop.is_set():
                planned = False
    finally:
        if proc is not None and proc.poll() is None:
            forced = True; errors.extend(force_owned(proc, owned))
        if not any(t.is_alive() for t in threads):
            raw.close(); logs.close()
    with guard: snapshot = list(events)
    report = assess(snapshot, batch, proc.returncode if proc else None, planned, forced, signal_time=signal_time)
    report.update(command=cmd, signal_time=signal_time, forced=forced, errors=errors,
                  expected_room_id=target, expected_anchor_uid=uid,
                  allow_unconfirmed_identity=args.allow_unconfirmed_identity,
                  startup_budget_sec=startup_budget, stop_grace_sec=STOP_GRACE)
    if errors:
        report['status']='失败'
    media = []
    for event in report['finished']:
        path = Path(event['file_path'])
        try:
            summary = media_summary(path, probe(path, tools))
            if not summary['has_video'] or not summary['has_audio'] or not summary['duration_sec']:
                raise ValueError('媒体缺轨或时长无效')
            media.append(summary)
        except Exception as exc:
            report['status']='失败'; errors.append(str(exc))
    report['media'] = media
    write_json(batch/'result.json', report)
    return 0 if report['status']=='正常通过' and not args.allow_unconfirmed_identity else 4

def main(argv=None):
    p = argparse.ArgumentParser(description='指定实例和目标的 Worker 切片/中断验证，默认不执行')
    p.add_argument('--vmindex', type=nonnegative, required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--target-kind', choices=['room_id','web_rid'], default='web_rid')
    p.add_argument('--expected-anchor-uid')
    p.add_argument('--web-rid')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--segment-duration', type=positive, default=20)
    p.add_argument('--record-seconds', type=positive, default=50)
    p.add_argument('--navigation-timeout', type=positive, default=300)
    p.add_argument('--allow-unconfirmed-identity', action='store_true')
    p.add_argument('--execute', action='store_true')
    args = p.parse_args(argv)
    if args.segment_duration < 5 or not 2*args.segment_duration+10 <= args.record_seconds <= 600:
        p.error('要求切片>=5秒，2*切片+10 <= 观察时长 <=600秒')
    if not 60 <= args.navigation_timeout <= 900 or not args.target.strip():
        p.error('导航预算须为60～900秒，目标不能为空')
    parent = output_parent(args.output_dir)
    if args.target_kind=='room_id' and (not args.target.isdigit() or not args.expected_anchor_uid) and not args.allow_unconfirmed_identity:
        p.error('内部 room_id 必须为数字，并明确提供独立主播 UID')
    if not args.execute:
        preview(args); return 0
    from src.mvp.dep_manager import DependencyManager
    dependency = DependencyManager()
    tools = dependency.get_media_tools()
    scrcpy = dependency.require_scrcpy()
    target, uid, web_rid = args.target.strip(), args.expected_anchor_uid, args.web_rid
    if args.target_kind == 'web_rid':
        from src.mvp.room_resolver import RoomResolver, RoomStatus
        resolver = RoomResolver(timeout=4)
        try:
            resolved = resolver.resolve(target)
        finally:
            resolver.session.close()
        if resolved.status != RoomStatus.LIVE or not resolved.room_id or not resolved.anchor_uid:
            raise ValueError('指定入口未确认在播或缺少身份，拒绝录制')
        if uid and uid != resolved.anchor_uid:
            raise ValueError('指定 UID 与独立查询冲突')
        target, uid, web_rid = resolved.room_id, resolved.anchor_uid, resolved.web_rid
        args.target_kind = 'room_id'
    batch = new_batch(parent, 'worker-e2e')
    print(f'本次材料：{batch}', flush=True)
    try:
        return run_worker(args, batch, target, uid, web_rid, tools, scrcpy)
    except BaseException as exc:
        write_json(batch/'failure.json', dict(error=str(exc)))
        raise

if __name__ == '__main__':
    sys.exit(entry(main))
