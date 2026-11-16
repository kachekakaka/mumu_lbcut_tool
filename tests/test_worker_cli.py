"""测试 Worker CLI 参数前置强校验与依赖规范 (Ticket 01)。"""
import pytest
from src.worker.cli import validate_args, build_arg_parser, main


def test_cli_parser_rejects_segment_greater_than_duration(capsys):
    """当 segment_duration 大于 total_duration (且 total_duration > 0) 时，必须以退出码 2 阻断退出。"""
    parser = build_arg_parser()
    args = parser.parse_args([
        "--vmindex", "0",
        "--target", "live_room_1",
        "--output-dir", "./test_out",
        "--segment-duration", "120",
        "--total-duration", "60"
    ])
    with pytest.raises(SystemExit) as exc_info:
        validate_args(args)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "segment" in captured.err.lower() or "duration" in captured.err.lower()


def test_cli_parser_rejects_negative_or_zero_values():
    """当 segment_duration <= 0 或 vmindex < 0 时，必须以退出码 2 阻断退出。"""
    parser = build_arg_parser()
    
    # segment <= 0
    args_zero_seg = parser.parse_args([
        "--vmindex", "0",
        "--target", "live_room_1",
        "--output-dir", "./test_out",
        "--segment-duration", "0"
    ])
    with pytest.raises(SystemExit) as exc_info:
        validate_args(args_zero_seg)
    assert exc_info.value.code == 2

    # total-duration < 0
    args_neg_duration = parser.parse_args([
        "--vmindex", "0",
        "--target", "live_room_1",
        "--output-dir", "./test_out",
        "--total-duration", "-10"
    ])
    with pytest.raises(SystemExit) as exc_info:
        validate_args(args_neg_duration)
    assert exc_info.value.code == 2


def test_cli_parser_rejects_empty_target(capsys):
    """当 target 仅包含空白字符或为空时，必须以退出码 2 阻断退出。"""
    parser = build_arg_parser()
    args = parser.parse_args([
        "--vmindex", "0",
        "--target", "   ",
        "--output-dir", "./test_out"
    ])
    with pytest.raises(SystemExit) as exc_info:
        validate_args(args)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "target" in captured.err.lower() or "直播间" in captured.err


def test_cli_parser_accepts_valid_parameters():
    """合规参数正常放行，无异常。"""
    parser = build_arg_parser()
    args = parser.parse_args([
        "--vmindex", "2",
        "--target", "7704666294802123554",
        "--expected-anchor-uid", "900001",
        "--output-dir", "./output",
        "--segment-duration", "60",
        "--total-duration", "180"
    ])
    # 正常不抛异常
    validate_args(args)


def test_cli_parser_window_modes():
    """验证窗口模式参数 (foreground/bottom/offscreen/hidden) 及位置坐标解析。"""
    parser = build_arg_parser()

    # 1. bottom 模式
    args_bottom = parser.parse_args([
        "--vmindex", "2",
        "--target", "room_1",
        "--output-dir", "./output",
        "--window-mode", "bottom",
    ])
    assert args_bottom.window_mode == "bottom"

    # 2. offscreen 模式加坐标
    args_offscreen = parser.parse_args([
        "--vmindex", "2",
        "--target", "room_1",
        "--output-dir", "./output",
        "--window-mode", "offscreen",
        "--window-pos-x", "-2500",
        "--window-pos-y", "100",
    ])
    assert args_offscreen.window_mode == "offscreen"
    assert args_offscreen.window_pos_x == -2500
    assert args_offscreen.window_pos_y == 100

    # 3. 非法窗口模式触发解析异常
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--vmindex", "2",
            "--target", "room_1",
            "--output-dir", "./output",
            "--window-mode", "invalid_mode",
        ])
