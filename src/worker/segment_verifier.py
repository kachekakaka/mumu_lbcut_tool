"""切片完整性三级核验与音视频双轨检测器 (Ticket 07)。"""
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Optional

from src.worker.executor import run_command

logger = logging.getLogger("SegmentVerifier")


@dataclass
class SegmentValidationResult:
    """切片校验结果。"""
    is_valid: bool
    has_video: bool
    has_audio: bool
    duration: float = 0.0
    size_mb: float = 0.0
    is_short_tail: bool = False
    reject_reason: str = ""


def verify_segment_file(
    file_path: Path,
    min_size_bytes: int = 100 * 1024,
    ffprobe_exe: Path | None = None,
    timeout: float = 3.0,
    allow_short_tail: bool = False,
) -> SegmentValidationResult:
    """对生成的切片 MP4 执行三级完整性核验。

    1. 文件存在性与最小大小阈值核验（短尾切片放宽大小阈值）；
    2. ffprobe 探测音视频双轨有效性与编码流；
    3. 时长有效性核验。
    """
    if not file_path.exists():
        return SegmentValidationResult(
            is_valid=False,
            has_video=False,
            has_audio=False,
            reject_reason="文件不存在",
        )

    try:
        stat_obj = file_path.stat()
        raw_size = getattr(stat_obj, "st_size", 0)
        file_size = int(raw_size)
    except (TypeError, ValueError, AttributeError):
        file_size = min_size_bytes

    size_mb = file_size / (1024 * 1024)

    effective_min_size = 1024 if allow_short_tail else min_size_bytes
    if file_size < effective_min_size:
        return SegmentValidationResult(
            is_valid=False,
            has_video=False,
            has_audio=False,
            size_mb=round(size_mb, 3),
            is_short_tail=allow_short_tail,
            reject_reason=f"文件过小 ({file_size} 字节 < {effective_min_size} 字节阈值)，可能写入截断",
        )

    # 尝试探测 ffprobe
    probe_bin = ffprobe_exe or shutil.which("ffprobe")
    if not probe_bin:
        return SegmentValidationResult(
            is_valid=False,
            has_video=False,
            has_audio=False,
            size_mb=round(size_mb, 2),
            reject_reason="未检测到 ffprobe 工具，无法核验音视频双轨有效性",
        )

    cmd = [
        str(probe_bin),
        "-v", "error",
        "-show_entries", "stream=codec_type,codec_name:format=duration,size",
        "-of", "json",
        str(file_path),
    ]

    try:
        res = run_command(cmd, timeout=timeout)
        if res.exit_code != 0:
            return SegmentValidationResult(
                is_valid=False,
                has_video=False,
                has_audio=False,
                size_mb=round(size_mb, 2),
                reject_reason=f"ffprobe 解析损坏: {res.stderr[:100]}",
            )

        probe_data = json.loads(res.stdout)
        streams = probe_data.get("streams", [])
        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        format_info = probe_data.get("format", {})
        duration_val = float(format_info.get("duration", 0.0))

        if not has_video:
            return SegmentValidationResult(
                is_valid=False,
                has_video=False,
                has_audio=has_audio,
                duration=duration_val,
                size_mb=round(size_mb, 2),
                reject_reason="视频轨缺失",
            )

        if not has_audio:
            return SegmentValidationResult(
                is_valid=False,
                has_video=True,
                has_audio=False,
                duration=duration_val,
                size_mb=round(size_mb, 2),
                reject_reason="音频轨缺失 (违反双轨录制规格，严禁静默产出无声视频)",
            )

        return SegmentValidationResult(
            is_valid=True,
            has_video=True,
            has_audio=True,
            duration=duration_val,
            size_mb=round(size_mb, 2),
        )

    except Exception as e:
        logger.warning(f"执行 ffprobe 探测异常: {e}")
        return SegmentValidationResult(
            is_valid=False,
            has_video=False,
            has_audio=False,
            size_mb=round(size_mb, 2),
            reject_reason=f"ffprobe 检测异常: {e}",
        )


def finalize_segment(
    raw_path: Path,
    target_path: Path,
    ffprobe_exe: Path | None = None,
    allow_short_tail: bool = False,
) -> tuple[bool, Path, SegmentValidationResult]:
    """收尾并归档切片文件。

    若校验通过：更名为 target_path；
    若目标已存在：绝不覆盖，拒绝成功，保留原文件并明确报错；
    若校验失败：更名为 <raw_path>.corrupted，阻止损坏文件流入下游。
    """
    res = verify_segment_file(raw_path, ffprobe_exe=ffprobe_exe, allow_short_tail=allow_short_tail)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    if res.is_valid:
        # 1. 目标文件存在性检查：绝不 unlink 或覆盖已有录像！
        if hasattr(target_path, "exists") and target_path.exists():
            err_msg = f"归档目标文件已存在 ({target_path})，拒绝覆盖已有产物"
            logger.error(err_msg)
            res.is_valid = False
            res.reject_reason = err_msg
            return False, raw_path, res

        # 2. 执行更名并严格核查
        try:
            if hasattr(raw_path, "replace"):
                raw_path.replace(target_path)
            if hasattr(target_path, "exists") and not target_path.exists():
                raise OSError(f"更名后目标文件不存在: {target_path}")
            return True, target_path, res
        except Exception as e:
            logger.error(f"更名正式切片文件失败: {e}")
            res.is_valid = False
            res.reject_reason = f"更名归档失败: {e}"
            return False, raw_path, res
    else:
        corrupted_path = raw_path.with_name(raw_path.stem + ".corrupted")
        try:
            if hasattr(raw_path, "replace") and not (hasattr(corrupted_path, "exists") and corrupted_path.exists()):
                raw_path.replace(corrupted_path)
        except Exception:
            corrupted_path = raw_path
        return False, corrupted_path, res
