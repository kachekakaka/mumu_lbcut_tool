"""持续分装与可见遮挡恢复：真实逻辑、隔离媒体/设备边界。"""
from pathlib import Path
import json
import shutil
import subprocess
import time
from unittest.mock import Mock
import pytest

from src.worker.continuous_pipeline import read_fragment_manifest, Fragment, ContinuousPipeline
from src.worker.navigator import WorkerNavigator
from src.mvp.dep_manager import MediaTools



def test_overlay_back_rechecks_and_does_not_leave_clean_live(monkeypatch):
    nav=WorkerNavigator.__new__(WorkerNavigator)
    commands=[]
    nav._adb=lambda *args,**kw: commands.append(args) or ''
    nav._navigation_sleep=lambda *a: True
    states=iter([{'activity':'LivePlayActivity','overlay':'live_panel_window','ui_available':True},
                 {'activity':'LivePlayActivity','overlay':None,'ui_available':True}])
    nav.inspect_live_surface=lambda **kw:next(states)
    assert nav.dismiss_live_overlay(time.monotonic()+10,lambda:False)
    assert commands==[('shell','input','keyevent','4')]
    nav.inspect_live_surface=lambda **kw:{'activity':'LivePlayActivity','overlay':None,'ui_available':True}
    commands.clear();assert nav.dismiss_live_overlay(time.monotonic()+10,lambda:False)
    assert commands==[]


def test_partial_manifest_is_not_closed(tmp_path):
    (tmp_path/'segments.csv').write_text('part_000001.mp4,0,6\npart_000002.mp4,6,',encoding='utf8')
    result=read_fragment_manifest(tmp_path)
    assert list(result)==[1] and result[1].closed
    assert result[1].end_pts==6


def test_production_capture_command_preserves_encoded_media(monkeypatch, tmp_path):
    import io
    from types import SimpleNamespace
    import src.worker.continuous_pipeline as module

    commands = []
    class Pipe:
        name = 'synthetic-pipe'
        error = None
        def __init__(self, job):
            pass
        def start(self, sink, tap):
            pass
    def spawn(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), poll=lambda: None)

    monkeypatch.setattr(module, 'NamedPipeBridge', Pipe)
    monkeypatch.setattr(module, 'StreamMonitor', Mock())
    monkeypatch.setattr(module, 'PipelineLogDrainer', Mock())
    monkeypatch.setattr(module.subprocess, 'Popen', spawn)
    pipeline = ContinuousPipeline('synthetic', tmp_path, 'scrcpy.exe', 'adb.exe', 2, 'ffmpeg.exe')
    def first_media():
        (pipeline.directory / 'part_000001.mp4').write_bytes(b'first'*240)
        return True
    pipeline.wait_hook = first_media
    try:
        pipeline.start()
        muxer, capture = commands
        def verify_commands(mux, recorder):
            assert {'--record-format=mkv', '--no-window', '--no-control'} <= set(recorder)
            assert mux[mux.index('-c') + 1] == 'copy' and '-r' not in mux
        verify_commands(muxer, capture)
        for flag in ('--record-format=mkv', '--no-window', '--no-control'):
            with pytest.raises(AssertionError):
                verify_commands(muxer, [arg for arg in capture if arg != flag])
        with pytest.raises(AssertionError):
            verify_commands([*muxer, '-r', '25'], capture)
    finally:
        pipeline._progress_thread.join(2)
        pipeline._mux_log.close()


def test_fragment_cursor_handles_half_line_rotation_and_replacement(tmp_path):
    p = ContinuousPipeline('synthetic', tmp_path, 'scrcpy.exe', 'adb.exe', 2, 'ffmpeg.exe')
    p.directory.mkdir()
    first = p.directory / 'part_000001.mp4'
    first.touch()
    assert p.fragment_updates() == [Fragment(1, first, False)]
    manifest = p.directory / 'segments.csv'
    manifest.write_bytes(b'part_000001.mp4,0,')
    assert p.fragment_updates() == []
    with manifest.open('ab') as stream:
        stream.write(b'2\n')
    second = p.directory / 'part_000002.mp4'
    second.touch()
    assert p.fragment_updates() == [Fragment(1, first, True, 0, 2), Fragment(2, second, False)]
    p.progress['out_time_us'] = '2600000'
    assert p.get_current_segment_elapsed() == pytest.approx(.6)
    assert p.fragment_updates() == []
    replacement = p.directory / 'rewritten.csv'
    replacement.write_bytes(b'part_000001.mp4,0,1\npart_000002.mp4,1,0.5\n')
    replacement.replace(manifest)
    assert [f.index for f in p.fragment_updates()] == [1, 2]
    assert p.timeline_invalid
    assert p.get_current_segment_elapsed() == pytest.approx(1.6)


def test_capture_error_after_total_duration_cutoff_is_tagged_expected(tmp_path,caplog):
    import logging
    from types import SimpleNamespace
    caplog.set_level(logging.INFO,logger='src.worker.continuous_pipeline')
    line='ERROR: Failed to write trailer to \\\\.\\pipe\\mumu_capture_x'
    p=ContinuousPipeline('synthetic',tmp_path,Path('scrcpy.exe'),Path('adb.exe'),2,
                         ffmpeg_exe=Path('ffmpeg.exe'),total_duration_sec=300)
    p._mux=SimpleNamespace(poll=lambda:None)
    p._log_capture_line(line)
    p._mux=SimpleNamespace(poll=lambda:0)
    p._log_capture_line(line)
    p.total_duration_sec=0
    p._log_capture_line(line)
    messages=[r.getMessage() for r in caplog.records]
    assert messages==['[capture] '+line,'[capture] (已录满总时长，预期收尾) '+line,'[capture] '+line]
    assert list(p.logs)==[line]*3


def test_real_controller_rotates_without_restarting_capture(monkeypatch,tmp_path,valid_media):
    from worker_harness import build_worker,assert_paired
    # 只替换 capture/muxer 输出边界；Controller、事件、封口校验、会话均真实执行。
    h=build_worker(monkeypatch,tmp_path,valid_media,total=7,segment=2)
    assert h.controller.run()==0
    events=h.events();terminals=assert_paired(events)
    assert len(terminals)>=3
    made=h.pipelines
    assert len(made)==1 and made[0].start_count==1 and made[0].stop_count==1
    assert sum(e.get('is_final',False) for e in terminals)==1
    assert all(e['event']=='segment_finished' for e in terminals)
    sessions=[e for e in events if e['event']=='capture_session']
    assert [e['action'] for e in sessions]==['started','stopped']


@pytest.mark.parametrize('flag',['--recording-mode','--overlap-duration'])
def test_worker_cli_has_no_restart_or_overlap_options(flag):
    from src.worker.cli import build_arg_parser
    with pytest.raises(SystemExit):
        build_arg_parser().parse_args(['--vmindex','2','--target','123','--output-dir','unused',flag,'1'])


def test_runtime_overlay_recovery_keeps_same_capture_process(tmp_path):
    from types import SimpleNamespace
    from src.worker.continuous_session import ContinuousSession
    from src.worker.state_machine import WorkerStateMachine,WorkerState
    now=time.monotonic()
    emitted=[]
    nav=SimpleNamespace(dismiss_live_overlay=Mock(return_value=True),wait_live_picture=Mock(return_value=True))
    c=SimpleNamespace(stop_requested=False,current_generation_id=1,_navigator=nav,_watchdog=None,
        config=SimpleNamespace(stall_timeout_sec=10),target_room_id='123',
        state_machine=WorkerStateMachine(WorkerState.RECORDING),
        emitter=SimpleNamespace(emit=emitted.append),action_dispatcher=SimpleNamespace(post_action=lambda a:None))
    class Watchdog:
        def __init__(self,**kw):pass
        def start(self):pass
    s=ContinuousSession(c,'serial',Path('scrcpy'),Path('adb'),
                        MediaTools(Path('ffmpeg.exe'),Path('ffprobe.exe')),watchdog_factory=Watchdog)
    s.pipe=object();original=s.pipe;s.epoch=1;s.last_clear_wall=time.time()-5;s.last_surface=now
    s.ledger={1:{'epoch':1,'terminal':False,'wall':time.time()}}
    sync_count=[0]
    def sync(**kwargs):
        sync_count[0]+=1
        if sync_count[0]>=2:
            # 面板关闭期间后台已切到新文件，风险事件必须覆盖后发现的文件。
            s.ledger[2]={'epoch':1,'terminal':False,'wall':time.time()}
    s.sync=sync
    from concurrent.futures import Future
    s.surface_future=Future();s.surface_future.set_result({'activity':'LivePlayActivity','overlay':'live_panel_window','ui_available':True})
    s.surface_gen=1
    try:
        s.poll_surface()
        assert s.pipe is original
        assert c.state_machine.state==WorkerState.RECORDING
        assert nav.dismiss_live_overlay.call_count==1
        assert [e.action for e in emitted if e.event=='overlay_recovery']==['detected','closed']
        assert {e.segment_index for e in emitted if e.event=='segment_quality_warning'}=={1,2}
    finally:
        s.probes.shutdown(wait=True);s.pool.shutdown(wait=True)


@pytest.mark.parametrize('history', [32, 128, 512, 1024])
def test_recording_iteration_avoids_history_when_media_is_unchanged(monkeypatch, tmp_path, history):
    import io
    import statistics
    from types import SimpleNamespace
    from src.worker.continuous_session import ContinuousSession
    from src.worker.controller import WorkerConfig, WorkerController
    from src.worker.events import WorkerEventEmitter

    stream = io.StringIO()
    controller = WorkerController(WorkerConfig(vmindex=77, target='123', output_dir=tmp_path,
        target_kind='room_id', expected_anchor_uid='456',
        heartbeat_interval_sec=.05), WorkerEventEmitter(stream))
    controller.target_room_id = '123'
    pipeline = ContinuousPipeline('fixture', tmp_path, 'scrcpy.exe', 'adb.exe', 2, 'ffmpeg.exe')
    pipeline.directory.mkdir()
    manifest = pipeline.directory / 'segments.csv'
    manifest.write_text(''.join(f'part_{idx:06d}.mp4,{idx-1},{idx}\n'
                                for idx in range(1, history+1)), encoding='utf-8')
    (pipeline.directory / f'part_{history+1:06d}.mp4').touch()
    pipeline.progress['out_time_us'] = str(history * 1_000_000 + 500_000)
    seeded = pipeline.fragment_updates()
    pipeline.monitor = SimpleNamespace(drain_events=lambda: [], fps=lambda sec: 15, unavailable=False,
                                       clock=lambda: 1.0)
    monkeypatch.setattr(pipeline, 'is_recording', lambda: True)

    cfg_time = [100.0]
    durations = []
    sync_durations = []
    last = time.perf_counter()
    def sleep(_seconds):
        nonlocal last
        durations.append((time.perf_counter() - last) * 1000)
        cfg_time[0] += .1
        last = time.perf_counter()
        if len(durations) >= 21:
            controller.request_stop()

    session = ContinuousSession(controller, 'fixture', 'scrcpy.exe', 'adb.exe',
        SimpleNamespace(), clock=lambda: cfg_time[0], sleeper=sleep)
    session.pipe, session.epoch = pipeline, 2
    session.latest_segment_index = history + 1
    class NoHistory(dict):
        def __iter__(self):
            pytest.fail('无变化循环访问了历史')
        items = values = keys = __iter__
        def __getitem__(self, key):
            if key <= history:
                pytest.fail(f'无变化循环访问了历史切片 {key}')
            return super().__getitem__(key)
    session.ledger = NoHistory({row.index: dict(epoch=2, terminal=row.closed) for row in seeded})
    pipeline._fragments = NoHistory(pipeline._fragments)
    session.identity_risks[2] = dict(lower=1.0, wall=1.0, flags={'identity_unconfirmed'}, marked=set())
    session.start_epoch = lambda: True
    session.poll_identity = lambda **kw: False
    session.poll_surface = lambda: None
    session.close = lambda: None
    original_sync = session.sync
    def timed_sync():
        started = time.perf_counter()
        original_sync()
        sync_durations.append((time.perf_counter() - started) * 1000)
    session.sync = timed_sync
    original_open = Path.open
    read_sizes = []

    class TracedManifest:
        def __init__(self, file):
            self.file = file
        def __enter__(self):
            self.file.__enter__()
            return self
        def __exit__(self, *args):
            return self.file.__exit__(*args)
        def __getattr__(self, key):
            return getattr(self.file, key)
        def read(self):
            data = self.file.read()
            read_sizes.append(len(data))
            return data
    def tracked_open(path, *args, **kwargs):
        file = original_open(path, *args, **kwargs)
        return TracedManifest(file) if path == manifest else file
    monkeypatch.setattr(Path, 'open', tracked_open)
    monkeypatch.setattr(Path, 'glob', lambda *args, **kwargs: pytest.fail('无变化循环枚举了目录'))
    try:
        assert session.run() == 0
        assert read_sizes == [0] * 21
        heartbeats = [json.loads(line) for line in stream.getvalue().splitlines()
                      if '"heartbeat"' in line]
        assert len(heartbeats) == 21
        print(f'历史 {history} 片，旧行访问 0，旧字节重读 0，目录枚举 0，'
              f'sync 中位数 {statistics.median(sync_durations[1:]):.3f} ms，'
              f'整轮处理耗时中位数 {statistics.median(durations[1:]):.3f} ms')
    finally:
        session.identity_probes.shutdown()
        session.probes.shutdown()
        session.pool.shutdown()


@pytest.mark.parametrize('flag',['--recording-mode','--overlap-duration'])
def test_scheduler_cli_has_no_restart_or_overlap_options(flag):
    from src.orchestrator.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(['run','--instances','2','--output-dir','output',flag,'1'])
