"""测试开录门禁原因分流与白名单弹窗穿透 (Ticket 08)。"""
from unittest.mock import MagicMock, patch
import pytest

from src.worker.controller import (
    WorkerController,
    WorkerConfig,
    EXIT_TARGET_INVALID,
)


def test_preflight_gate_unresolved_short_link_rejected(capsys):
    """检测到未清洗的短链时，Worker 严格拒绝执行并返回 EXIT_TARGET_INVALID (2)。"""
    config = WorkerConfig(
        vmindex=0,
        target="https://v.douyin.com/iANgXXXX/",
        output_dir=MagicMock(),
    )
    controller = WorkerController(config=config)
    code = controller.run()
    assert code == EXIT_TARGET_INVALID
    captured = capsys.readouterr()
    assert "目标非法阻断" in captured.err or "需由上层调度平台解析" in captured.err


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
