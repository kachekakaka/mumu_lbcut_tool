"""脚本的参数、独立输出和有界外部工具边界。导入不探测设备。"""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError('必须为正整数')
    return n

def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError('实例索引必须非负')
    return n

def input_file(value):
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f'输入文件不存在：{path}')
    return path

def output_parent(value):
    path = Path(value).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise ValueError('输出必须位于仓库之外')
    if path.exists() and not path.is_dir():
        raise ValueError('输出父路径不是目录')
    return path

def new_batch(parent, label):
    parent = output_parent(parent)
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / (label + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex)
    path.mkdir(exist_ok=False)
    return path

def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)

def preview(args, **extra):
    print(json.dumps(dict(vars(args), **extra, executed=False,
                         notice='仅本地检查；设备、网络和运行依赖待执行阶段核验'),
                     ensure_ascii=False, default=str))

def entry(main):
    try:
        return main() or 0
    except KeyboardInterrupt:
        print('已取消', file=sys.stderr)
        return 130
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 3

def media_tools():
    from src.mvp.dep_manager import DependencyManager
    return DependencyManager().get_media_tools()

def probe(path, tools=None, timeout=15):
    from src.mvp.command import run_client
    tools = tools or media_tools()
    path = input_file(path)
    result = run_client([str(tools.ffprobe), '-v', 'error', '-show_format',
                         '-show_streams', '-of', 'json', str(path)], timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'ffprobe 失败：{result.stderr}')
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get('streams'), list):
        raise ValueError('ffprobe 返回结构无效')
    return data

def media_summary(path, data):
    def duration(value):
        try:
            n = float(value)
            import math
            return n if math.isfinite(n) and n >= 0 else None
        except (ValueError, TypeError):
            return None
    video = next((s for s in data['streams'] if s.get('codec_type') == 'video'), None)
    audio = next((s for s in data['streams'] if s.get('codec_type') == 'audio'), None)
    vd = duration(video.get('duration')) if video else None
    ad = duration(audio.get('duration')) if audio else None
    return dict(file=str(path), duration_sec=duration(data.get('format', {}).get('duration')),
                has_video=video is not None, has_audio=audio is not None, video=video, audio=audio,
                track_duration_difference_ms=round(abs(vd-ad)*1000, 2) if vd is not None and ad is not None else None,
                notice='轨道时长差不是音画同步误差；未执行完整解码、音频隔离或遮挡验收')
