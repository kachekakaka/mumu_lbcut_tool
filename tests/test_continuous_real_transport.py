"""真实 Windows 命名管道 + ffmpeg 无重编码分段；不是 MuMu 实机测试。"""
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
import pytest
from src.worker.continuous_pipeline import ContinuousPipeline,NamedPipeBridge


@pytest.mark.skipif(os.name!='nt',reason='当前产品传输为 Windows 命名管道')
def test_realtime_segments_preserve_every_encoded_packet(monkeypatch,tmp_path,valid_media):
    ff=shutil.which('ffmpeg');probe=shutil.which('ffprobe')
    reference=tmp_path/'transport-reference.mkv'
    original=NamedPipeBridge.start
    class Tee:
        def __init__(self,sink):self.sink=sink;self.file=reference.open('xb')
        def write(self,data):
            written=self.sink.write(data)
            if written:self.file.write(data[:written])
            return written
        def flush(self):self.sink.flush();self.file.flush()
        def close(self):self.file.close();self.sink.close()
    monkeypatch.setattr(NamedPipeBridge,'start',lambda self,sink:original(self,Tee(sink)))
    def producer(pipe,directory):
        return subprocess.Popen([ff,'-hide_banner','-loglevel','error','-y','-re','-stream_loop','10',
            '-i',str(valid_media),'-t','11','-map','0','-c','copy','-f','matroska',pipe],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    p=ContinuousPipeline('synthetic',tmp_path/'out',Path('scrcpy.exe'),Path('adb.exe'),
                         segment_duration_sec=2,producer_factory=producer)
    try:
        p.start();capture=p._proc.pid;mux=p._mux.pid
        before_stop=set();deadline=time.monotonic()+20
        while p._proc.poll() is None and time.monotonic()<deadline:
            assert p._proc.pid==capture and p._mux.pid==mux
            before_stop.update(f.index for f in p.fragments() if f.closed)
            time.sleep(.1)
        assert p.stop(),p.failure_details()
        fragments=p.fragments();assert len(fragments)>=3 and len(before_stop)>=2
        assert all(f.closed for f in fragments)
        def hashes(path,stream):
            data=json.loads(subprocess.check_output([probe,'-v','error','-select_streams',stream,
                '-show_packets','-show_data_hash','sha256','-show_entries','packet=data_hash',
                '-of','json',str(path)],timeout=10))
            return [packet['data_hash'] for packet in data['packets']]
        for stream in ('v:0','a:0'):
            expected=hashes(reference,stream)
            actual=[h for f in fragments for h in hashes(f.path,stream)]
            assert expected and actual==expected
        for a,b in zip(fragments,fragments[1:]):assert abs(a.end_pts-b.start_pts)<.0001
    finally:
        p.stop()


def test_pipe_cancel_without_producer_releases_thread(tmp_path):
    if os.name!='nt':pytest.skip('Windows only')
    import io
    bridge=NamedPipeBridge();bridge.start(io.BytesIO());bridge.cancel.set()
    assert bridge.join(2)
    assert bridge.done.is_set()
