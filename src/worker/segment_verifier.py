"""切片双轨/容器校验与不覆盖归档；短尾片大小不是完整性判据。"""
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
from src.mvp.dep_manager import DependencyManager

from src.worker.executor import run_command

logger = logging.getLogger(__name__)


@dataclass
class SegmentValidationResult:
    is_valid: bool
    has_video: bool
    has_audio: bool
    duration: float = 0.0
    size_mb: float = 0.0
    is_short_tail: bool = False
    reject_reason: str = ""


def verify_segment_file(file_path: Path, min_size_bytes: int = 0,
                        ffprobe_exe: Path | None = None, timeout: float = 8.0,
                        allow_short_tail: bool = False, *, require_mp4: bool = False) -> SegmentValidationResult:
    """元数据完整性不是逐帧解码保证；min_size_bytes 不再作为拒绝有效媒体的门槛。"""
    result = SegmentValidationResult(False, False, False, is_short_tail=allow_short_tail)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        result.reject_reason = f"文件不存在或不可读取: {exc}"
        return result
    result.size_mb = size / (1024 * 1024)
    if size == 0:
        result.reject_reason = "零字节空文件，写入截断"
        return result
    try:
        probe = ffprobe_exe or DependencyManager().get_media_tools().ffprobe
    except (OSError, RuntimeError) as exc:
        result.reject_reason = f"固定媒体工具不可用：{exc}"
        return result
    cmd = [str(probe), "-v", "error"]
    if require_mp4:
        cmd += ["-f", "mp4"]
    cmd += ["-show_entries",
           "stream=codec_type,codec_name,duration,nb_frames:format=duration,size",
           "-of", "json", str(file_path)]
    try:
        output = run_command(cmd, timeout=timeout)
        if output.exit_code != 0:
            raise ValueError(f"ffprobe 解析损坏: {output.stderr[:160]}")
        data = json.loads(output.stdout)
        streams = data.get("streams", [])
        result.has_video = any(s.get("codec_type") == "video" for s in streams)
        result.has_audio = any(s.get("codec_type") == "audio" for s in streams)
        result.duration = float(data.get("format", {}).get("duration", 0))
        if not result.has_video:
            result.reject_reason = "视频轨缺失"
        elif not result.has_audio:
            result.reject_reason = "音频轨缺失"
        elif not math.isfinite(result.duration) or result.duration <= 0:
            result.reject_reason = "媒体时长无效"
        elif any(s.get("nb_frames") == "0" for s in streams if s.get("codec_type") in ("video", "audio")):
            result.reject_reason = "必需轨道没有有效媒体帧"
        else:
            result.is_valid = True
    except Exception as exc:
        result.reject_reason = f"ffprobe 检测异常: {exc}"
    return result


def finalize_segment(raw_path: Path, target_path: Path, ffprobe_exe: Path | None = None,
                     allow_short_tail: bool = False, timeout: float = 8.0):
    """成功时无覆盖归档；失败保留原始路径和证据，绝不删除已有产物。"""
    result = verify_segment_file(raw_path, ffprobe_exe=ffprobe_exe,
                                 allow_short_tail=allow_short_tail, timeout=timeout)
    if not result.is_valid:
        return False, raw_path, result
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.resolve() == target_path.resolve():
        return True, raw_path, result
    try:
        # 同目录硬链接创建是原子的：已存在目标必然失败，不存在检查/替换窗口。
        os.link(raw_path, target_path)
        try:
            raw_path.unlink()
        except OSError:
            # 两个名字指向同一有效产物，保留原始名字比误删数据更安全。
            logger.warning("归档成功但原始名字未移除: %s", raw_path)
        return True, target_path, result
    except OSError as exc:
        result.is_valid = False
        result.reject_reason = f"归档失败（目标已存在或不支持无覆盖链接），原文件保留: {exc}"
        return False, raw_path, result
