"""测试开录门禁原因分流与白名单弹窗穿透 (Ticket 08)。"""
from unittest.mock import MagicMock, patch
import pytest

from src.worker.controller import (
    WorkerController,
    WorkerConfig,
    EXIT_TARGET_OFFLINE,
    EXIT_GATE_NETWORK_ERROR,
)
from src.worker.resolver import RoomStatus, ResolvedRoom


def test_preflight_gate_anchor_offline_returns_exit_code_10(capsys):
    """确诊主播未开播时，以退出码 10 退出，并输出'主播未开播'。"""
    config = WorkerConfig(
        vmindex=0,
        target="https://live.douyin.com/123456",
        output_dir=MagicMock(),
    )
    controller = WorkerController(config=config)

    with patch("src.worker.controller.WorkerRoomResolver") as mock_resolver_cls:
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = ResolvedRoom(
            web_rid="123456",
            status=RoomStatus.ENDED,  # 确诊下播
        )
        mock_resolver_cls.return_value = mock_resolver

        code = controller.run()
        assert code == EXIT_TARGET_OFFLINE  # 10
        captured = capsys.readouterr()
        assert "主播未开播" in captured.err or "未开播" in captured.err


def test_preflight_gate_network_error_returns_exit_code_11(capsys):
    """检测接口断网或返回 500 导致无法确定时，以退出码 11 退出，输出'门禁检测服务异常'。"""
    config = WorkerConfig(
        vmindex=0,
        target="https://live.douyin.com/123456",
        output_dir=MagicMock(),
    )
    controller = WorkerController(config=config)

    with patch("src.worker.controller.WorkerRoomResolver") as mock_resolver_cls:
        mock_resolver = MagicMock()
        mock_resolver.resolve.return_value = ResolvedRoom(
            web_rid="123456",
            status=RoomStatus.UNKNOWN,  # 异常无法确认
        )
        mock_resolver_cls.return_value = mock_resolver

        code = controller.run()
        assert code == EXIT_GATE_NETWORK_ERROR  # 11
        captured = capsys.readouterr()
        assert "门禁检测服务异常" in captured.err or "服务异常" in captured.err


def test_popup_bypass_handles_youth_mode_dialog():
    """遇到青少年模式等白名单弹窗时，穿透器识别并点击关闭/我知道了。"""
    from src.worker.popup_bypasser import PopupBypasser

    mock_adb = MagicMock()
    # 模拟 UI dump 包含青少年模式
    ui_dump_with_youth_mode = """
    <node text="为呵护未成年人健康成长，抖音特别推出青少年模式" bounds="[100,500][900,1000]">
        <node text="我知道了" resource-id="com.ss.android.ugc.aweme:id/confirm_btn" bounds="[200,1100][800,1200]" />
    </node>
    """
    mock_adb.run_adb.return_value = MagicMock(exit_code=0, stdout=ui_dump_with_youth_mode, stderr="")

    bypasser = PopupBypasser(adb_client=mock_adb)
    handled = bypasser.detect_and_dismiss_popups()
    assert handled is True
    # 验证向坐标发起了 tap
    mock_adb.run_adb.assert_any_call("shell", "input", "tap", "500", "1150", timeout=pytest.approx(3.0, 1.0))
