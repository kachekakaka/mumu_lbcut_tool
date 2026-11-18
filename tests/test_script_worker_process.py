"""使用真实 Windows 子进程验证脚本的中断和超时；子进程只复制合成媒体。"""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import scripts.verify_worker_e2e_live as runner


@pytest.mark.skipif(os.name != 'nt', reason='Windows 进程组信号')
@pytest.mark.parametrize('ignore_stop', [False, True])
def test_wrapper_signals_only_its_child_and_classifies_result(tmp_path, monkeypatch, valid_media, media_tools, ignore_stop):
    batch = tmp_path/'run'
    batch.mkdir()
    child = tmp_path/'fake_worker.py'
    child.write_text("""
import json,os,shutil,signal,sys,time,uuid
from pathlib import Path
batch=Path(sys.argv[1])
source=Path(sys.argv[2])
task=batch/'synthetic'
task.mkdir()
paths=[task/'123_synthetic_000001.mp4',task/'123_synthetic_000002.mp4']
for p in paths: shutil.copyfile(source,p)
def emit(kind,**kw):
    print(json.dumps(dict(event=kind,event_id=uuid.uuid4().hex,run_id='synthetic',timestamp=time.time(),**kw)),flush=True)
def stop(*_):
    if sys.argv[3]=='True': return
    emit('segment_finished',segment_index=2,file_path=str(paths[1]),is_final=True)
    emit('worker_stopped',exit_code=0,reason='stop_requested',had_segment_errors=False)
    sys.exit(0)
signal.signal(signal.SIGBREAK,stop)
emit('worker_started',pid=os.getpid(),target='123')
emit('segment_started',segment_index=1,file_path=str(task/'capture_test'/'part_000001.mp4'))
emit('segment_finished',segment_index=1,file_path=str(paths[0]),is_final=False)
emit('segment_started',segment_index=2,file_path=str(task/'capture_test'/'part_000002.mp4'))
while True: time.sleep(.05)
""", encoding='utf-8')
    original = subprocess.Popen
    launched = []
    def launch(cmd, **kw):
        if '-m' in cmd and 'src.worker.cli' in cmd:
            cmd = [sys.executable, str(child), str(batch), str(valid_media), str(ignore_stop)]
            proc = original(cmd, **kw)
            launched.append(proc)
            return proc
        return original(cmd, **kw)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(runner, 'STOP_GRACE', .5 if ignore_stop else 3)
    args = SimpleNamespace(vmindex=2, target_kind='room_id', segment_duration=20,
                           navigation_timeout=60, record_seconds=.3, allow_unconfirmed_identity=False)
    result = runner.run_worker(args,batch,'123','456',None,media_tools,tmp_path/'scrcpy.exe')
    report = json.loads((batch/'result.json').read_text(encoding='utf-8'))
    assert len(launched)==1 and launched[0].poll() is not None
    assert (batch/'events.ndjson').is_file() and (batch/'worker.log').is_file()
    if ignore_stop:
        assert result==4 and report['forced'] and report['status']=='失败'
    else:
        assert result==0 and report['status']=='正常通过'
        assert not report['forced'] and len(report['media'])==2
