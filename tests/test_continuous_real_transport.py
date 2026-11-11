"""真实 Windows 命名管道 + ffmpeg 无重编码分段；不是 MuMu 实机测试。"""
import hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
import pytest
import src.worker.stream_monitor as stream_monitor
from src.worker.continuous_pipeline import ContinuousPipeline,NamedPipeBridge


@pytest.mark.skipif(os.name!='nt',reason='当前产品传输为 Windows 命名管道')
@pytest.mark.parametrize('analyzer',['real','stuck'])
def test_realtime_segments_preserve_every_encoded_packet(monkeypatch,tmp_path,valid_media,analyzer):
    ff=shutil.which('ffmpeg');probe=shutil.which('ffprobe')
    spawned=[]
    original_init=stream_monitor.Analyzer.__init__
    def track(self,*args,**kwargs):
        original_init(self,*args,**kwargs);spawned.append(self)
    monkeypatch.setattr(stream_monitor.Analyzer,'__init__',track)
    if analyzer=='stuck':
        # 分析一路存活但从不读输入：录像写入不得受反压。
        monkeypatch.setattr(stream_monitor,'build_command',
            lambda exe:[sys.executable,'-c','import time;time.sleep(120)'])
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
    monkeypatch.setattr(NamedPipeBridge,'start',lambda self,sink,tap=None:original(self,Tee(sink),tap))
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
        assert p.bridge.bytes_forwarded==reference.stat().st_size
        assert (p.monitor.frame_seq>0)==(analyzer=='real')
        assert spawned and all(a.proc.poll() is not None for a in spawned)
    finally:
        p.stop()


@pytest.mark.skipif(os.name!='nt',reason='当前产品传输为 Windows 命名管道')
def test_total_duration_is_cut_by_muxer_on_media_timeline(tmp_path):
    ff=shutil.which('ffmpeg');probe=shutil.which('ffprobe')
    if not ff or not probe:pytest.skip('需要 ffmpeg/ffprobe')
    # 与 scrcpy 输出一致：Constrained Baseline、无 B 帧、2 秒一个关键帧（分装器 -t 按 DTS 截止）。
    source=tmp_path/'baseline.mp4'
    subprocess.run([ff,'-v','error','-n','-f','lavfi','-i','testsrc=size=96x64:rate=8','-f','lavfi',
        '-i','sine=frequency=1000:sample_rate=24000','-t','2','-c:v','libx264','-profile:v','baseline',
        '-g','16','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(source)],check=True,capture_output=True,timeout=20)
    def producer(pipe,directory):
        # 生产者比总时长多录 4 秒：截止只能来自分装器，而不是采集端停止。
        return subprocess.Popen([ff,'-hide_banner','-loglevel','error','-y','-re','-stream_loop','20',
            '-i',str(source),'-t','12','-map','0','-c','copy','-f','matroska',pipe],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    p=ContinuousPipeline('synthetic',tmp_path/'out',Path('scrcpy.exe'),Path('adb.exe'),
                         segment_duration_sec=4,producer_factory=producer,total_duration_sec=8)
    elapsed=[]
    try:
        p.start();deadline=time.monotonic()+30
        while not p.reached_total_duration() and time.monotonic()<deadline:
            p.fragments();elapsed.append(p.get_current_segment_elapsed())
            assert p._mux.poll() is None or p._mux.poll()==0,p.failure_details()
            time.sleep(.1)
        assert p.reached_total_duration(),p.failure_details()
        assert p._proc.poll() is None,'采集端仍在运行时分装器已按总时长结束'
        assert p.stop(),p.failure_details()
        fragments=p.fragments()
        assert [f.index for f in fragments]==[1,2] and all(f.closed for f in fragments)
        assert 7.8<=fragments[-1].end_pts<=8.05
        total=sum(float(subprocess.check_output([probe,'-v','error','-show_entries','format=duration',
            '-of','csv=p=0',str(f.path)],text=True,timeout=10)) for f in fragments)
        assert 7.5<=total<=8.1
        # 分装器进度约每秒刷新一次；换片归零的计算另有确定性用例。
        assert max(elapsed)>0
    finally:
        p.stop()


def test_segment_elapsed_counts_from_last_sealed_cut(tmp_path):
    from src.worker.continuous_pipeline import Fragment
    p=ContinuousPipeline.__new__(ContinuousPipeline)
    p.progress={};p._fragments={}
    assert p.get_current_segment_elapsed()==0
    p.progress['out_time_us']='4200000'
    p._fragments={1:Fragment(1,tmp_path/'part_000001.mp4',False)}
    assert p.get_current_segment_elapsed()==4.2
    p.progress['out_time_us']='5100000'
    p._fragments={1:Fragment(1,tmp_path/'a',True,0.0,4.0),2:Fragment(2,tmp_path/'b',False)}
    assert round(p.get_current_segment_elapsed(),3)==1.1
    p.progress['out_time_us']='N/A';p.progress['out_time_ms']='6000000'
    assert round(p.get_current_segment_elapsed(),3)==2.0


def test_pipe_cancel_without_producer_releases_thread(tmp_path):
    if os.name!='nt':pytest.skip('Windows only')
    import io
    bridge=NamedPipeBridge();bridge.start(io.BytesIO());bridge.cancel.set()
    assert bridge.join(2)
    assert bridge.done.is_set()
