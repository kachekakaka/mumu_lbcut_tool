"""录像流画面分析：字节桥把 MKV 非阻塞复制给独立 ffmpeg，解码成灰度小图。

分析一路的任何故障都不能反压或中断录像写入：队列满了只丢分析数据。分析进程可单独
重启，重启时先写缓存的 MKV 文件头，再从下一个 Cluster 起点接入。
"""
from collections import deque
from dataclasses import dataclass
import logging
import os
import subprocess
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

FRAME_W, FRAME_H = 160, 288
FRAME_BYTES = FRAME_W * FRAME_H

SEGMENT_ID = 0x18538067
CLUSTER_ID = 0x1F43B675
# Segment 的一级子元素；未知长度的 Cluster 遇到它们即结束。
SEGMENT_CHILD_IDS = {0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1C53BB6B, 0x1254C367,
                     0x1043A770, 0x1941A469, CLUSTER_ID}


class MkvClusterTracker:
    """按 EBML 元素结构增量解析，只定位 Cluster 起点；不在负载字节中搜索标识。"""
    MAX_HEADER_BYTES = 1 << 20

    def __init__(self):
        self.pos = 0
        self.header = bytearray()
        self.header_done = False
        self.error = None
        self._pending = b''
        self._skip = 0
        self._in_cluster = False

    @staticmethod
    def _vint_len(first):
        for n in range(8):
            if first & (0x80 >> n):
                return n + 1
        return 0

    def feed(self, data):
        """返回本块中 Cluster 起点列表 [(绝对偏移, 起点在上一块中的前缀字节)]。"""
        mv = memoryview(data)
        base, n = self.pos, len(mv)
        self.pos += n
        if self.error:
            return []
        if not self.header_done:
            self.header += mv
        starts = []
        i = 0
        try:
            while i < n:
                if self._skip:
                    step = min(self._skip, n - i)
                    self._skip -= step
                    i += step
                    continue
                pending = self._pending
                head = pending + bytes(mv[i:i + 12 - len(pending)])
                id_len = self._vint_len(head[0])
                if not 1 <= id_len <= 4:
                    raise ValueError('EBML 元素 ID 无效')
                if len(head) < id_len + 1:
                    self._pending, i = head, n
                    break
                size_len = self._vint_len(head[id_len])
                if not size_len:
                    raise ValueError('EBML 元素长度无效')
                header_len = id_len + size_len
                if len(head) < header_len:
                    self._pending, i = head, n
                    break
                element_id = int.from_bytes(head[:id_len], 'big')
                raw = head[id_len:header_len]
                size = int.from_bytes(raw, 'big') & ((1 << (7 * size_len)) - 1)
                unknown = size == (1 << (7 * size_len)) - 1
                start = base + i - len(pending)
                self._pending = b''
                i += header_len - len(pending)
                if element_id == SEGMENT_ID:
                    self._in_cluster = False
                    continue
                if element_id == CLUSTER_ID:
                    starts.append((start, pending if start < base else b''))
                    if not self.header_done:
                        del self.header[start:]
                        self.header_done = True
                    self._in_cluster = unknown
                    if not unknown:
                        self._skip = size
                    continue
                if self._in_cluster and element_id in SEGMENT_CHILD_IDS:
                    self._in_cluster = False
                if unknown:
                    raise ValueError('非 Segment/Cluster 元素长度未知')
                self._skip = size
            if not self.header_done and len(self.header) > self.MAX_HEADER_BYTES:
                raise ValueError('MKV 文件头超过上限仍未出现 Cluster')
        except ValueError as exc:
            self.error = str(exc)
            return []
        return starts


def build_command(ffmpeg_exe):
    return [str(ffmpeg_exe), '-hide_banner', '-loglevel', 'error', '-nostdin',
            # 低延迟解码：默认探测与帧线程会让输出滞后输入约半秒以上。
            # 不用 nobuffer：它会丢弃探测期缓存的包，接入后首个 GOP 全部丢失。
            '-fflags', '+discardcorrupt', '-flags', 'low_delay',
            '-probesize', '32768', '-analyzeduration', '0', '-f', 'matroska', '-i', 'pipe:0',
            '-map', '0:v:0', '-an', '-sn', '-dn', '-threads', '1',
            '-vf', f'scale={FRAME_W}:{FRAME_H}:flags=fast_bilinear,format=gray',
            # rawvideo 默认按恒定帧率补帧；静止或卡顿时会伪造新帧，必须直通。
            '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1']


class Analyzer:
    """一个分析 ffmpeg 进程：有界输入队列、写线程、按整帧读取的读线程。"""

    def __init__(self, ffmpeg_exe, on_frame, max_queue_bytes, stderr_file=None):
        self.failed = None
        self.fed_at = None
        self.bytes_at_fed = 0
        self.frames = 0
        self.first_frame_at = None
        self.started_at = 0.0
        self._on_frame = on_frame
        self._max = max_queue_bytes
        self._queue = deque()
        self._queued = 0
        self._cond = threading.Condition()
        self._closed = False
        cmd = build_command(ffmpeg_exe)
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=stderr_file or subprocess.DEVNULL, bufsize=0,
                                     creationflags=flags)
        self.pid = self.proc.pid
        self._writer = threading.Thread(target=self._write_loop, name='stream-monitor-in', daemon=True)
        self._reader = threading.Thread(target=self._read_loop, name='stream-monitor-out', daemon=True)
        self._writer.start()
        self._reader.start()

    def put(self, payload):
        with self._cond:
            if self._closed:
                return True
            if self._queued + len(payload) > self._max:
                return False
            self._queue.append(payload)
            self._queued += len(payload)
            self._cond.notify()
            return True

    def fail(self, reason):
        if self.failed is None and not self._closed:
            self.failed = reason

    def _write_loop(self):
        stdin = self.proc.stdin
        try:
            while True:
                with self._cond:
                    while not self._queue and not self._closed:
                        self._cond.wait()
                    if self._closed:
                        return
                    item = self._queue.popleft()
                    self._queued -= len(item)
                view = memoryview(item)
                while view:
                    wrote = stdin.write(view)
                    if not wrote:
                        raise BrokenPipeError('分析进程拒绝输入')
                    view = view[wrote:]
        except (OSError, ValueError):
            self.fail('analyzer_input_broken')

    def _read_loop(self):
        out = self.proc.stdout
        buf = bytearray(FRAME_BYTES)
        view = memoryview(buf)
        try:
            while True:
                got = 0
                while got < FRAME_BYTES:
                    n = out.readinto(view[got:])
                    if not n:
                        self.fail('analyzer_exited')
                        return
                    got += n
                self._on_frame(self, bytes(buf))
        except (OSError, ValueError):
            self.fail('analyzer_output_broken')

    def kill(self):
        """不等待；可在持锁时调用。"""
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()
        try:
            self.proc.kill()
        except OSError:
            pass

    def close(self):
        self.kill()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        self._writer.join(timeout=2)
        self._reader.join(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


@dataclass(frozen=True)
class MonitorView:
    available: bool
    frame_seq: int
    frame: np.ndarray | None
    frame_at: float | None
    frozen_since: float | None


class StreamMonitor:
    """归属一代采集会话；看门狗重建不影响它，采集会话重建时随之新建。"""

    def __init__(self, ffmpeg_exe, stderr_path=None, clock=time.monotonic,
                 max_queue_bytes=8 << 20, no_frame_sec=10.0, verify_sec=5.0,
                 max_restarts=3, retry_sec=60.0, trial_timeout_sec=15.0,
                 healthy_reset_sec=30.0, analyzer_factory=None):
        self.ffmpeg_exe = ffmpeg_exe
        self.stderr_path = stderr_path
        self.clock = clock
        self.max_queue_bytes = max_queue_bytes
        self.no_frame_sec, self.verify_sec = no_frame_sec, verify_sec
        self.max_restarts, self.retry_sec = max_restarts, retry_sec
        self.trial_timeout_sec, self.healthy_reset_sec = trial_timeout_sec, healthy_reset_sec
        self.analyzer_factory = analyzer_factory or self._spawn
        self.tracker = MkvClusterTracker()
        self.analyzer = None
        self.restarts = 0
        self.unavailable = False
        self.next_retry_at = None
        self.bytes_in = 0
        self.frame_seq = 0
        self.latest_frame = None
        self.last_frame_at = None
        self.available_since = None
        self.frozen_since = None
        self._verify = None
        self._no_frame_floor = None
        self._need_join = True
        self._frame_times = deque()
        self._events = deque(maxlen=200)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._stderr = None
        self._feed_error_logged = False
        # 旧分析进程在锁外回收：读线程回调也要拿锁，持锁等待会连带阻塞字节桥。
        self._retired = []

    # ---- 生命周期 ----
    def start(self):
        if self.stderr_path:
            self._stderr = open(self.stderr_path, 'ab')
        with self._lock:
            self._launch(self.clock(), 'initial')
        self._thread = threading.Thread(target=self._loop, name='stream-monitor', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        with self._lock:
            analyzer, self.analyzer = self.analyzer, None
            if analyzer:
                self._retire(analyzer)
        self._reap()
        if self._stderr:
            self._stderr.close()
            self._stderr = None

    def _loop(self):
        while not self._stop.wait(.5):
            try:
                self.tick()
            except Exception:
                log.exception('录像流分析监督异常')

    def _retire(self, analyzer):
        analyzer.kill()
        self._retired.append(analyzer)

    def _reap(self):
        with self._lock:
            retired, self._retired = self._retired, []
        for analyzer in retired:
            analyzer.close()

    def _spawn(self):
        return Analyzer(self.ffmpeg_exe, self._on_frame, self.max_queue_bytes, self._stderr)

    def _event(self, action, reason='', analyzer=None):
        self._events.append({'action': action, 'reason': reason,
                             'analyzer_pid': getattr(analyzer, 'pid', 0) or 0})

    def drain_events(self):
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def _launch(self, now, reason):
        old, self.analyzer = self.analyzer, None
        if old:
            self._retire(old)
        self._need_join = True
        self.available_since = None
        try:
            analyzer = self.analyzer_factory()
        except Exception as exc:
            log.warning('启动录像流分析进程失败: %s', exc)
            self._event('analyzer_start_failed', str(exc))
            return
        analyzer.started_at = now
        self.analyzer = analyzer
        self._event('analyzer_started', reason, analyzer)

    def _go_unavailable(self, now, reason):
        old, self.analyzer = self.analyzer, None
        if old:
            self._retire(old)
        self.unavailable = True
        self.next_retry_at = now + self.retry_sec
        self.available_since = None
        self.frozen_since = None
        self._verify = None
        self._event('unavailable', reason)

    def _on_failure(self, now, reason):
        self._event('analyzer_failed', reason, self.analyzer)
        self._verify = None
        if self.restarts >= self.max_restarts:
            self._go_unavailable(now, reason)
        else:
            self.restarts += 1
            self._launch(now, reason)

    # ---- 字节桥与读线程回调 ----
    def feed(self, data):
        """在字节桥线程调用；不阻塞、不抛异常。"""
        try:
            base = self.tracker.pos
            starts = self.tracker.feed(data)
            with self._lock:
                self.bytes_in += len(data)
                analyzer = self.analyzer
                if analyzer is None or analyzer.failed or self._stop.is_set():
                    return
                joined = False
                if self._need_join:
                    if not self.tracker.header_done or not starts:
                        return
                    offset, prefix = starts[0]
                    payload = bytes(self.tracker.header) + prefix + bytes(data[max(0, offset - base):])
                    self._need_join = False
                    joined = True
                else:
                    payload = bytes(data)
                if not analyzer.put(payload):
                    analyzer.fail('queue_overflow')
                    return
                if joined:
                    analyzer.fed_at = self.clock()
                    analyzer.bytes_at_fed = self.bytes_in
        except Exception:
            if not self._feed_error_logged:
                self._feed_error_logged = True
                log.exception('录像流分析复制异常；录像写入不受影响')

    def _on_frame(self, analyzer, data):
        now = self.clock()
        with self._lock:
            if analyzer is not self.analyzer:
                return
            analyzer.frames += 1
            if analyzer.first_frame_at is None:
                analyzer.first_frame_at = now
            if self.available_since is None and not self.unavailable:
                self.available_since = now
            self._frame_times.append(now)
            while self._frame_times and self._frame_times[0] < now - 61:
                self._frame_times.popleft()
            self.latest_frame = np.frombuffer(data, dtype=np.uint8).reshape(FRAME_H, FRAME_W)
            self.frame_seq += 1
            self.last_frame_at = now
            self.frozen_since = None

    # ---- 监督 ----
    def tick(self, now=None):
        try:
            self._tick(self.clock() if now is None else now)
        finally:
            self._reap()

    def _tick(self, now):
        with self._lock:
            if self._stop.is_set():
                return
            analyzer = self.analyzer
            if self.unavailable:
                self._tick_unavailable(now, analyzer)
                return
            if analyzer is None:
                self._on_failure(now, 'analyzer_start_failed')
                return
            if analyzer.failed:
                self._on_failure(now, analyzer.failed)
                return
            if self._verify:
                self._tick_verify(now, analyzer)
                return
            if (self.restarts and analyzer.first_frame_at is not None
                    and now - analyzer.first_frame_at >= self.healthy_reset_sec):
                self.restarts = 0
            if self.frozen_since is not None or analyzer.fed_at is None:
                return
            ref = max(t for t in (analyzer.fed_at, self.last_frame_at, self._no_frame_floor) if t is not None)
            if now - ref >= self.no_frame_sec:
                # 先换一个分析进程核实，分清是分析卡住还是录像本身没有新画面。
                self._verify = {'origin': ref, 'started': now}
                self._event('verify_no_frames', f'{now - ref:.1f}s', analyzer)
                self._launch(now, 'verify_no_frames')

    def _tick_verify(self, now, analyzer):
        verify = self._verify
        if analyzer.frames:
            self._verify = None
            self._event('analyzer_stalled', 'new_analyzer_produced_frames', analyzer)
            self.restarts += 1
            return
        if analyzer.fed_at is not None and now - analyzer.fed_at >= self.verify_sec:
            self._verify = None
            if self.bytes_in > analyzer.bytes_at_fed:
                self.frozen_since = verify['origin']
                self._event('no_new_frames_confirmed', f'since={verify["origin"]:.1f}', analyzer)
            else:
                # 录像字节也停了：交给文件增长检测，不计冻结。
                self._no_frame_floor = now
                self._event('verify_inconclusive', 'no_capture_bytes', analyzer)
            return
        if analyzer.fed_at is None and now - verify['started'] >= self.no_frame_sec:
            self._verify = None
            self._no_frame_floor = now
            self._event('verify_inconclusive', 'analyzer_not_fed', analyzer)

    def _tick_unavailable(self, now, analyzer):
        if analyzer is None:
            if now >= self.next_retry_at:
                self._launch(now, 'retry')
                if self.analyzer is None:
                    self.next_retry_at = now + self.retry_sec
            return
        if analyzer.frames:
            self.unavailable = False
            self.restarts = 0
            self.available_since = analyzer.first_frame_at
            self._event('recovered', '', analyzer)
        elif analyzer.failed or now - analyzer.started_at >= self.trial_timeout_sec:
            self._event('retry_failed', analyzer.failed or 'no_frames', analyzer)
            self.analyzer = None
            self._retire(analyzer)
            self.next_retry_at = now + self.retry_sec

    # ---- 查询 ----
    def view(self):
        with self._lock:
            return MonitorView(not self.unavailable, self.frame_seq, self.latest_frame,
                               self.last_frame_at, self.frozen_since)

    def fps(self, window_sec, now=None):
        """窗口内实际帧率；分析未连续覆盖整个窗口时为 None。"""
        now = self.clock() if now is None else now
        with self._lock:
            if self.unavailable or self.available_since is None or now - self.available_since < window_sec:
                return None
            count = sum(1 for t in self._frame_times if t > now - window_sec)
            return count / window_sec

    @property
    def analyzer_pid(self):
        analyzer = self.analyzer
        return getattr(analyzer, 'pid', 0) or 0
