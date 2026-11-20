"""单个 scrcpy 连续编码，命名管道传递 MKV，ffmpeg 无重编码分装 MP4。

计划切片只由 segment muxer 在关键帧处分装；不重启采集、不复制重叠内容。
实际切点依赖关键帧，不能承诺任意秒数精确切割。此传输当前仅支持 Windows。
设置总时长时由分装器在录像时间轴上截止（-t），最后一片在总时长处封口。
"""
from collections import deque
from dataclasses import dataclass
import csv
import ctypes
import ctypes.wintypes as W
import io
import math
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import uuid

from src.worker.scrcpy_probe import PipelineLogDrainer
from src.worker.stream_monitor import StreamMonitor

log = logging.getLogger(__name__)

SEGMENT_TIME_DELTA = 0.05


class NamedPipeBridge:
    """有界内存的单写者字节桥；不创建长期增长的中间录像文件。"""
    def __init__(self, job_id=None):
        if os.name != 'nt':
            raise RuntimeError('连续采集命名管道当前只支持 Windows')
        prefix = str(job_id) + '_' if job_id and re.fullmatch(r'[0-9a-f]{32}', job_id) else ''
        self.name = r'\\.\pipe\mumu_capture_' + prefix + uuid.uuid4().hex
        self.bytes_forwarded = 0
        self.error = None
        self.done = threading.Event()
        self.cancel = threading.Event()
        self._thread = None
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.CreateNamedPipeW.argtypes = [W.LPCWSTR,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.DWORD,W.LPVOID]
        self.k.CreateNamedPipeW.restype = W.HANDLE
        self.k.ConnectNamedPipe.argtypes = [W.HANDLE,W.LPVOID]
        self.k.ConnectNamedPipe.restype = W.BOOL
        self.k.ReadFile.argtypes = [W.HANDLE,W.LPVOID,W.DWORD,ctypes.POINTER(W.DWORD),W.LPVOID]
        self.k.ReadFile.restype = W.BOOL
        self.k.CloseHandle.argtypes = [W.HANDLE]
        self.k.CloseHandle.restype = W.BOOL
        # PIPE_ACCESS_INBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE, BYTE/NOWAIT.
        self.handle = self.k.CreateNamedPipeW(self.name, 0x80001, 1, 1, 1048576, 1048576, 0, None)
        if self.handle == W.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def start(self, sink, tap=None):
        """tap 收到同一份字节；它必须非阻塞且不抛异常，不能拖住封装输入。"""
        if self._thread:
            raise RuntimeError('字节桥不允许重复启动')
        def pump():
            buf, count = ctypes.create_string_buffer(65536), W.DWORD()
            try:
                while not self.cancel.is_set():
                    self.k.ConnectNamedPipe(self.handle, None)
                    if self.k.ReadFile(self.handle, buf, len(buf), ctypes.byref(count), None):
                        if not count.value:
                            self.cancel.wait(.01)
                            continue
                        data = memoryview(buf.raw)[:count.value]
                        if tap:
                            tap(data)
                        while data:
                            wrote = sink.write(data)
                            if wrote is None or wrote <= 0:
                                raise BrokenPipeError('分装进程拒绝输入')
                            data = data[wrote:]
                            self.bytes_forwarded += wrote
                        sink.flush()
                    else:
                        error = ctypes.get_last_error()
                        if error in (232,536):  # 暂无数据/等待唯一生产者连接。
                            self.cancel.wait(.01)
                            continue
                        if error == 109:  # 生产者关闭；正常 EOF，不能抢先截断。
                            break
                        raise OSError(error, '命名管道读取失败')
            except Exception as exc:
                self.error = str(exc)
            finally:
                try:
                    sink.close()
                except OSError:
                    pass
                self.k.CloseHandle(self.handle)
                self.handle = None
                self.done.set()
        self._thread = threading.Thread(target=pump, name='continuous-mkv-bridge', daemon=True)
        self._thread.start()

    def join(self, timeout):
        if self._thread:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        if self.handle is not None:
            self.k.CloseHandle(self.handle)
            self.handle = None
        self.done.set()
        return True


@dataclass(frozen=True)
class Fragment:
    index: int
    path: Path
    closed: bool
    start_pts: float | None = None
    end_pts: float | None = None


def read_fragment_manifest(directory: Path) -> dict[int, Fragment]:
    """仅换行完整的 CSV 记录表示 muxer 已封口；文件存在不是完成证据。"""
    result = {}
    path = directory / 'segments.csv'
    if not path.exists():
        return result
    data = path.read_bytes()
    complete = data[:data.rfind(b'\n')+1]
    return _parse_fragment_rows(directory, complete)


def _parse_fragment_rows(directory, complete):
    result = {}
    for row in csv.reader(io.StringIO(complete.decode('utf-8'))):
        if len(row) != 3:
            raise ValueError('分装完成清单格式错误')
        name, start, end = row
        match = re.fullmatch(r'part_(\d+)\.mp4', Path(name).name)
        if not match:
            raise ValueError('分装清单出现未知文件名')
        idx = int(match.group(1))
        result[idx] = Fragment(idx, directory / Path(name).name, True, float(start), float(end))
    return result


class ContinuousPipeline:
    """一代采集会话只有一个 scrcpy 和一个 segmenter；故障恢复才另起一代。"""
    def __init__(self, serial, output_dir, scrcpy_exe, adb_exe, segment_duration_sec,
                 ffmpeg_exe, max_size=1280, bit_rate='4M', max_fps=None,
                 start_index=1, producer_factory=None, total_duration_sec=0):
        self.serial, self.output_dir = serial, Path(output_dir).resolve()
        self.scrcpy_exe, self.adb_exe = Path(scrcpy_exe).resolve(), Path(adb_exe).resolve()
        self.ffmpeg_exe = str(ffmpeg_exe)
        if segment_duration_sec <= 0 or start_index < 1:
            raise ValueError('分段时长和起始序号必须为正数')
        self.segment_duration_sec = segment_duration_sec
        self.total_duration_sec = max(0.0, float(total_duration_sec or 0))
        self.limit_reached = False
        self.start_index = start_index
        self.max_size, self.bit_rate, self.max_fps = max_size, bit_rate, max_fps
        self.producer_factory = producer_factory
        self.directory = self.output_dir / ('capture_' + uuid.uuid4().hex)
        self._proc = self._mux = None
        self.bridge = None
        self.monitor = None
        self.wait_hook = None
        self.started_at = 0.0
        self._stopped = False
        self.forced_stop = False
        self._capture_drainer = None
        self._progress_thread = None
        self._mux_log = None
        self.logs = deque(maxlen=40)
        self.progress = {}
        self.command = []
        self._fragments = {}
        self._manifest_id = None
        self._manifest_offset = 0
        self._manifest_tail = b''
        self._last_cut_pts = 0.0
        self.timeline_invalid = False
        self.manifest_rebuilt = False
        self.current_file_path = None
        self.current_segment_index = 0

    def start(self, timeout_sec=25):
        if self._proc or self._mux:
            raise RuntimeError('连续采集实例不可重用')
        self.directory.mkdir(parents=True, exist_ok=False)
        self.bridge = NamedPipeBridge(self.output_dir.parent.name)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        self._mux_log = (self.directory/'segmenter.stderr.log').open('xb')
        limit = []
        if self.total_duration_sec:
            # 切点允许提前 delta；截止点同样提前 delta，避免恰在总时长前的关键帧开出极短尾片。
            limit = ['-t', f'{max(.1, self.total_duration_sec - SEGMENT_TIME_DELTA):.3f}']
        # MuMu 的 AAC-LC 包没有关键包标记；只放行音频的初始非关键包，保留视频关键帧门禁。
        mux_cmd = [self.ffmpeg_exe,'-hide_banner','-loglevel','warning','-nostdin','-n',
            '-f','matroska','-i','pipe:0','-map','0:v:0','-map','0:a:0','-c','copy','-copyinkf:a',*limit,
            '-f','segment','-segment_time',str(self.segment_duration_sec),
            '-segment_time_delta',str(SEGMENT_TIME_DELTA),'-reset_timestamps','1',
            '-segment_start_number',str(self.start_index),'-segment_format','mp4',
            '-segment_list',str(self.directory/'segments.csv'),'-segment_list_type','csv',
            '-progress','pipe:1',str(self.directory/'part_%06d.mp4')]
        try:
            self._mux = subprocess.Popen(mux_cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=self._mux_log,bufsize=0,creationflags=flags)
            self.monitor = StreamMonitor(self.ffmpeg_exe, stderr_path=self.directory/'analysis.stderr.log')
            self.monitor.start()
            self.bridge.start(self._mux.stdin, tap=self.monitor.feed)
            def progress():
                try:
                    for raw in iter(self._mux.stdout.readline,b''):
                        key,sep,value=raw.decode('utf8','replace').strip().partition('=')
                        if sep:self.progress[key]=value
                finally:
                    self._mux.stdout.close()
            self._progress_thread=threading.Thread(target=progress,name='continuous-progress',daemon=True)
            self._progress_thread.start()
            cmd=[str(self.scrcpy_exe),'-s',self.serial,'--record='+self.bridge.name,
                '--record-format=mkv','--no-playback','--no-window','--no-control',
                '--audio-source=playback','--audio-codec=aac','--video-codec=h264',
                '--require-audio','--display-id=0','--video-bit-rate='+self.bit_rate,
                '--max-size='+str(self.max_size),'--video-codec-options=i-frame-interval=2']
            if self.max_fps:cmd.append('--max-fps='+str(self.max_fps))
            env=dict(os.environ,ADB=str(self.adb_exe))
            env['PATH']=str(self.adb_exe.parent)+os.pathsep+env.get('PATH','')
            server_path = self.scrcpy_exe.parent / 'scrcpy-server'
            if server_path.exists() and 'SCRCPY_SERVER_PATH' not in env:
                env['SCRCPY_SERVER_PATH'] = str(server_path)
            self.command=cmd
            self.capture_launch_at=time.time()
            if self.producer_factory:
                self._proc=self.producer_factory(self.bridge.name,self.directory)
            else:
                self._proc=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    text=True,encoding='utf8',errors='replace',env=env,creationflags=flags,
                    cwd=str(self.adb_exe.parent))
            self._capture_drainer=PipelineLogDrainer(self._proc,on_log_line=self._log_capture_line)
            self._capture_drainer.start()
            self.started_at=time.monotonic()
            until=self.started_at+timeout_sec
            while time.monotonic()<until:
                if self.wait_hook and self.wait_hook() is False:
                    raise InterruptedError('启动连续采集时收到停止请求')
                if not self.is_recording():
                    raise RuntimeError('连续采集启动失败: '+self.failure_details())
                first = self.directory / f'part_{self.start_index:06d}.mp4'
                if first.is_file() and first.stat().st_size > 1024:
                    self.first_media_observed_at=time.time()
                    return
                time.sleep(.05)
            raise TimeoutError('持续采集未在预算内产生可见媒体进度')
        except BaseException:
            self.stop(timeout_sec=8)
            raise

    def _log_capture_line(self, line):
        self.logs.append(line)
        # 分装器按总时长正常结束后关闭管道，scrcpy 随后写不进收尾数据并报 ERROR，属预期收尾。
        if self.total_duration_sec and self._mux and self._mux.poll()==0:
            log.info('[capture] (已录满总时长，预期收尾) %s',line)
        else:
            log.info('[capture] %s',line)

    def fragments(self):
        self.fragment_updates()
        return [self._fragments[i] for i in sorted(self._fragments)]

    def fragment_updates(self, full=False):
        """只读取新增 CSV 字节并探查下一片；结束或清单换代时核对目录。"""
        path = self.directory / 'segments.csv'
        try:
            manifest = path.open('rb')
        except FileNotFoundError:
            full = full or self._manifest_id is not None
            self._manifest_id = None
            self._manifest_offset = 0
            self._manifest_tail = b''
            completed = {}
        else:
            with manifest:
                info = os.fstat(manifest.fileno())
                identity = (info.st_dev, info.st_ino)
                full = full or (self._manifest_id is not None and (
                    identity != self._manifest_id or info.st_size < self._manifest_offset))
                offset = 0 if full else self._manifest_offset
                manifest.seek(offset)
                data = (b'' if full else self._manifest_tail) + manifest.read()
                last_newline = data.rfind(b'\n') + 1
                completed = _parse_fragment_rows(self.directory, data[:last_newline]) if last_newline else {}
                self._manifest_id = identity
                self._manifest_offset = manifest.tell()
                self._manifest_tail = data[last_newline:]

        self.manifest_rebuilt = full
        if full:
            self._last_cut_pts = 0.0
            current = {}
            for raw in self.directory.glob('part_*.mp4'):
                if match := re.fullmatch(r'part_(\d+)\.mp4', raw.name):
                    idx = int(match.group(1))
                    current[idx] = Fragment(idx, raw, False)
            current.update(completed)
            # 已观察的未封片即使原文件消失，收尾仍须为它写入终态。
            for idx, old in self._fragments.items():
                if idx not in current:
                    current[idx] = Fragment(idx, old.path, False)
            self._fragments = current
            self.current_segment_index = max(current, default=0)
        else:
            current = dict(completed)
            # CSV 可以一次封好数片，打开的文件一定紧接最后已出现的序号。
            last = max(self.current_segment_index, max(completed, default=0))
            next_index = last + 1 if last else self.start_index
            candidate = self.directory / f'part_{next_index:06d}.mp4'
            if candidate.is_file():
                current[next_index] = Fragment(next_index, candidate, False)
                last = next_index
            self._fragments.update(current)
            self.current_segment_index = last
        if self.current_segment_index:
            self.current_file_path = self._fragments[self.current_segment_index].path
        for idx in sorted(completed):
            f = completed[idx]
            if (not math.isfinite(f.start_pts) or not math.isfinite(f.end_pts)
                    or f.start_pts < 0 or f.end_pts < max(f.start_pts, self._last_cut_pts)):
                self.timeline_invalid = True
            elif f.closed:
                self._last_cut_pts = f.end_pts
        return [current[i] for i in sorted(current)]

    def is_recording(self):
        return bool(self._proc and self._mux and self._proc.poll() is None
                    and self._mux.poll() is None and self.bridge and not self.bridge.error)

    def get_exit_code(self):
        if self.limit_reached:
            # 分装器截止后字节桥写入失败、scrcpy 随之报错都是预期收尾，只看分装器是否正常结束。
            return 0 if self._mux and self._mux.poll()==0 else 30
        if self.forced_stop or (self.bridge and self.bridge.error):return 30
        codes=[p.poll() for p in (self._proc,self._mux) if p]
        if any(c not in (None,0) for c in codes):return next(c for c in codes if c not in (None,0))
        return 0 if len(codes)==2 and all(c==0 for c in codes) else None

    def get_current_file_size_mb(self):
        # 传输进度单调累积，物理切片换文件不能触发文件倒退误报。
        return (self.bridge.bytes_forwarded if self.bridge else 0)/1048576

    def media_time(self):
        """分装器已写出的录像时间轴位置（秒）；尚无进度时为 None。"""
        for key in ('out_time_us','out_time_ms'):  # ffmpeg 的 out_time_ms 实际也是微秒
            try:
                return max(0.0,int(self.progress.get(key))/1e6)
            except (TypeError,ValueError):
                continue
        return None

    def get_current_segment_elapsed(self):
        media=self.media_time()
        if media is None:return 0.0
        return max(0.0,media-self._last_cut_pts)

    def reached_total_duration(self):
        """分装器按 -t 正常结束即为录满总时长；其他原因退出仍按采集故障处理。"""
        if self.limit_reached:return True
        if not self.total_duration_sec or not self._mux or self._mux.poll()!=0:return False
        if self._progress_thread:self._progress_thread.join(timeout=2)
        media=self.media_time()
        self.limit_reached=media is not None and media>=self.total_duration_sec-1.0
        return self.limit_reached

    def failure_details(self):
        tail=''
        try:tail=(self.directory/'segmenter.stderr.log').read_text(encoding='utf8',errors='replace')[-1200:]
        except OSError:pass
        return f'capture={self._proc.poll() if self._proc else None}, mux={self._mux.poll() if self._mux else None}; bridge={self.bridge.error if self.bridge else None}; '+tail+'; '+' | '.join(self.logs)[-1200:]

    def stop(self, timeout_sec=15):
        if self._stopped:return self.get_exit_code()==0
        deadline=time.monotonic()+timeout_sec
        graceful_deadline=max(time.monotonic(),deadline-2)
        # 先停止唯一采集进程，等 MKV 字节桥自然 EOF，再让封装器写完最后一片。
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
        while self._proc and self._proc.poll() is None and time.monotonic()<graceful_deadline:
            if self.wait_hook:self.wait_hook()
            time.sleep(.05)
        if self._proc and self._proc.poll() is None:
            self.forced_stop=True;self._proc.kill();self._proc.wait(timeout=max(.001,deadline-time.monotonic()))
        if self.bridge:
            if not self.bridge.join(max(0,deadline-time.monotonic())):
                self.forced_stop=True;self.bridge.cancel.set()
        if self._mux:
            try:self._mux.wait(timeout=max(.001,graceful_deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                self.forced_stop=True;self._mux.kill();self._mux.wait(timeout=max(.001,deadline-time.monotonic()))
        if self.bridge:
            self.bridge.cancel.set()
            if not self.bridge.join(max(0,deadline-time.monotonic())):raise RuntimeError('分装字节桥未退出，禁止释放实例')
        if self._capture_drainer:self._capture_drainer.join(timeout=max(0,deadline-time.monotonic()))
        if self._progress_thread:self._progress_thread.join(timeout=max(0,deadline-time.monotonic()))
        self.capture_stopped_at=time.time()
        self.capture_stopped_mono=time.monotonic()
        if self.monitor:self.monitor.stop()
        if self._mux_log:self._mux_log.close()
        self._stopped=True
        return self.get_exit_code()==0

    def stop_segment(self, timeout_sec=15):
        return self.stop(timeout_sec)

    def teardown(self):
        return self.stop()
