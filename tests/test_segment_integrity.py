"""测试切片产物完整性与音视频双轨校验 (Ticket 07)。"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from src.worker.segment_verifier import (
    verify_segment_file,
    finalize_segment,
    SegmentValidationResult,
)


def test_verify_segment_file_rejects_zero_or_tiny_file(tmp_path):
    """0 字节或极小截断文件必须判定为无效损坏文件。"""
    tiny_file = tmp_path / "tiny.mp4"
    tiny_file.write_bytes(b"\x00" * 50)  # 50 字节

    result = verify_segment_file(tiny_file, min_size_bytes=1024)
    assert not result.is_valid
    assert result.reject_reason  # 依据容器解析失败，而非固定大小门槛


def test_verify_segment_file_rejects_missing_audio(tmp_path):
    """仅有视频轨而缺失音频轨的切片，必须识别出缺少音频并拒绝。"""
    fake_mp4 = tmp_path / "video_only.mp4"
    fake_mp4.write_bytes(b"\x00" * 150000)

    # 模拟 ffprobe 仅检测到 1 个 video 流，缺少 audio 流
    mock_probe_output = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"}
        ],
        "format": {
            "duration": "60.0",
            "size": "150000"
        }
    }

    with patch("src.worker.segment_verifier.run_command") as mock_run:
        mock_run.return_value = MagicMock(exit_code=0, stdout=json.dumps(mock_probe_output), stderr="")
        result = verify_segment_file(fake_mp4, ffprobe_exe=Path("ffprobe.exe"))
        assert not result.is_valid
        assert result.has_video is True
        assert result.has_audio is False
        assert "音频轨缺失" in result.reject_reason or "audio" in result.reject_reason


def test_verify_segment_file_accepts_valid_dual_track(tmp_path):
    """音视频双轨完整且时长合规的文件判定为有效。"""
    valid_mp4 = tmp_path / "valid.mp4"
    valid_mp4.write_bytes(b"\x00" * 200000)

    mock_probe_output = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac"}
        ],
        "format": {
            "duration": "180.5",
            "size": "200000"
        }
    }

    with patch("src.worker.segment_verifier.run_command") as mock_run:
        mock_run.return_value = MagicMock(exit_code=0, stdout=json.dumps(mock_probe_output), stderr="")
        result = verify_segment_file(valid_mp4, ffprobe_exe=Path("ffprobe.exe"))
        assert result.is_valid
        assert result.has_video is True
        assert result.has_audio is True
        assert result.duration == 180.5


def test_finalize_segment_archives_success_and_preserves_failure(tmp_path):
    """校验通过时无覆盖归档，失败时保留原始文件并准确返回失败。"""
    raw_file = tmp_path / "recording_tmp.mp4"
    raw_file.write_bytes(b"dummy_data_padding" * 100)
    target_final = tmp_path / "record_final_001.mp4"

    # 1. 成功场景
    with patch("src.worker.segment_verifier.verify_segment_file") as mock_verify:
        mock_verify.return_value = SegmentValidationResult(
            is_valid=True,
            has_video=True,
            has_audio=True,
            duration=60.0,
            size_mb=1.0,
            reject_reason=""
        )
        ok, final_path, res = finalize_segment(raw_file, target_final)
        assert ok is True
        assert final_path == target_final
        assert target_final.exists()
        assert not raw_file.exists()

    # 2. 失败场景
    bad_raw = tmp_path / "bad_tmp.mp4"
    bad_raw.write_bytes(b"bad_data")
    bad_target = tmp_path / "record_final_002.mp4"

    with patch("src.worker.segment_verifier.verify_segment_file") as mock_verify:
        mock_verify.return_value = SegmentValidationResult(
            is_valid=False,
            has_video=True,
            has_audio=False,
            duration=10.0,
            size_mb=0.1,
            reject_reason="缺少音频"
        )
        ok, final_path, res = finalize_segment(bad_raw, bad_target)
        assert ok is False
        assert final_path == bad_raw
        assert bad_raw.read_bytes() == b"bad_data"
        assert final_path.exists()
        assert not bad_target.exists()


def test_valid_small_tail_uses_actual_ffprobe(tmp_path, valid_media):
    import shutil
    raw = tmp_path / "valid_small.mp4"
    shutil.copyfile(valid_media, raw)
    result = verify_segment_file(raw, min_size_bytes=10 * 1024 * 1024, allow_short_tail=True)
    assert result.is_valid and result.has_video and result.has_audio
    assert result.duration > 0
    assert result.is_short_tail


def test_archive_conflict_preserves_both_files(tmp_path, valid_media):
    import shutil
    raw = tmp_path / "raw.mp4"
    target = tmp_path / "existing.mp4"
    shutil.copyfile(valid_media, raw)
    target.write_bytes(b"existing-valuable-recording")
    before = raw.read_bytes()
    ok, path, result = finalize_segment(raw, target, allow_short_tail=True)
    assert not ok and path == raw
    assert raw.read_bytes() == before
    assert target.read_bytes() == b"existing-valuable-recording"
