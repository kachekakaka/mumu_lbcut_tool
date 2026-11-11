"""复现 MuMu/scrcpy 的 AAC 无关键标记输入，经过真实命名管道验证无损分装。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from src.worker.continuous_pipeline import ContinuousPipeline


def clear_audio_key_flags(data):
    data = bytearray(data)
    changed = []
    def vint(pos, keep=False):
        mask, length = 128, 1
        while not data[pos] & mask:
            mask >>= 1
            length += 1
        value = int.from_bytes(data[pos:pos+length], 'big')
        return (value if keep else value & ((1 << (7*length))-1)), length
    def walk(start, end):
        pos = start
        while pos < end:
            eid, n = vint(pos, True)
            size, m = vint(pos+n)
            body = pos+n+m
            stop = end if size == (1 << (7*m))-1 else body+size
            assert body <= stop <= end
            if eid in (0x18538067, 0x1F43B675):
                walk(body, stop)
            elif eid == 0xA0:
                # lavc 的最后一个 AAC padding 包使用 BlockGroup。将该组改为
                # 同长度 Void，夹具只保留与 scrcpy 相同的 SimpleBlock 音频。
                child = body
                while child < stop:
                    cid, cn = vint(child, True)
                    csize, cm = vint(child+cn)
                    if cid == 0xA1:
                        assert vint(child+cn+cm)[0] == 2
                        data[pos] = 0xEC
                        break
                    child += cn+cm+csize
            elif eid == 0xA3:
                track, width = vint(body)
                if track == 2 and data[body+width+2] & 128:
                    data[body+width+2] &= 127
                    changed.append(body+width+2)
            pos = stop
    walk(0, len(data))
    assert changed
    return bytes(data)


@pytest.mark.skipif(os.name != 'nt', reason='产品使用 Windows 命名管道')
def test_unmarked_aac_packets_survive_real_pipeline(tmp_path, media_tools):
    ff, probe = str(media_tools.ffmpeg), str(media_tools.ffprobe)
    source = tmp_path/'unmarked.mkv'
    encoded = subprocess.check_output([ff, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=96x64:rate=10',
        '-f', 'lavfi', '-i', 'sine=frequency=997:sample_rate=48000', '-t', '9', '-map', '0:v:0', '-map', '1:a:0',
        '-c:v', 'libx264', '-profile:v', 'baseline', '-g', '20', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
        '-write_crc32', '0', '-f', 'matroska', 'pipe:1'], timeout=25)
    source.write_bytes(clear_audio_key_flags(encoded))
    def packets(path, selector):
        data = subprocess.check_output([probe, '-v', 'error', '-select_streams', selector,
            '-show_packets', '-show_data', '-show_entries', 'packet=data,flags,dts', '-of', 'json', str(path)], timeout=10)
        return json.loads(data)['packets']
    expected_audio = packets(source, 'a:0')
    assert expected_audio and all('K' not in packet['flags'] for packet in expected_audio)
    # 原命令确实复制 0 个音频包，防止夹具退化为无法重现问题的普通 MKV。
    control = tmp_path/'unfixed.mp4'
    subprocess.run([ff, '-v', 'error', '-n', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0',
                    '-c', 'copy', str(control)], check=True, capture_output=True, timeout=15)
    assert not packets(control, 'a:0')
    script = tmp_path/'producer.py'
    script.write_text('''import pathlib,sys,time
data=pathlib.Path(sys.argv[1]).read_bytes()
with open(sys.argv[2],'wb',buffering=0) as out:
    for offset in range(0,len(data),1024):
        out.write(data[offset:offset+1024])
        time.sleep(9*min(1024,len(data)-offset)/len(data))
''', encoding='utf-8')
    def producer(pipe, directory):
        return subprocess.Popen([sys.executable, str(script), str(source), pipe], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    pipeline = ContinuousPipeline('synthetic', tmp_path/'recordings', Path('scrcpy.exe'), Path('adb.exe'),
                                  2, ffmpeg_exe=media_tools.ffmpeg, producer_factory=producer)
    try:
        pipeline.start()
        deadline = time.monotonic()+20
        while pipeline._proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        assert pipeline.stop(), pipeline.failure_details()
        fragments = pipeline.fragments()
        assert len(fragments) >= 3 and all(f.closed for f in fragments)
        for selector in ('a:0', 'v:0'):
            original = [packet['data'] for packet in packets(source, selector)]
            actual = []
            for fragment in fragments:
                items = packets(fragment.path, selector)
                assert items
                dts = [packet['dts'] for packet in items if 'dts' in packet]
                assert dts == sorted(dts)
                actual.extend(packet['data'] for packet in items)
            assert actual == original
        for fragment in fragments:
            result = subprocess.run([ff, '-v', 'error', '-i', str(fragment.path), '-map', '0:v:0', '-map', '0:a:0',
                '-fps_mode', 'passthrough', '-enc_time_base:v', 'demux', '-f', 'null', '-'], capture_output=True, timeout=15)
            assert result.returncode == 0 and not result.stderr, result.stderr
    finally:
        pipeline.stop()
