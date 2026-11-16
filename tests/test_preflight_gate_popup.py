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
        allow_unconfirmed_identity=True,  # 本用例验证输入清洗；双标识配置校验另测。
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
    <hierarchy><node text="为呵护未成年人健康成长，抖音特别推出青少年模式" bounds="[100,500][900,1000]">
        <node text="我知道了" resource-id="com.ss.android.ugc.aweme:id/confirm_btn" bounds="[200,1100][800,1200]" />
    </node></hierarchy>
    """
    mock_adb.run_adb.return_value = MagicMock(exit_code=0, stdout=ui_dump_with_youth_mode, stderr="")

    bypasser = PopupBypasser(adb_client=mock_adb)
    handled = bypasser.detect_and_dismiss_popups()
    assert handled is True
    # 验证向坐标发起了 tap
    mock_adb.run_adb.assert_any_call("shell", "input", "tap", "500", "1150", timeout=pytest.approx(3.0, 1.0))


def test_popup_bypass_taps_agree_not_disagree_on_first_launch_privacy_dialog():
    """抖音首次启动的个人信息保护指引：“不同意”排在“同意”之前，必须点“同意”。"""
    from src.worker.popup_bypasser import PopupBypasser

    mock_adb = MagicMock()
    privacy = """
    <hierarchy><node text="" bounds="[0,0][1080,1920]">
        <node text="个人信息保护指引" bounds="[180,560][900,620]" />
        <node text="欢迎使用抖音！我们将通过《用户服务协议》与《基础服务数据处理规则》帮助你了解" bounds="[180,640][900,1300]" />
        <node text="不同意" bounds="[440,1340][640,1400]" />
        <node text="同意" bounds="[120,1420][960,1520]" />
    </node></hierarchy>
    """
    mock_adb.run_adb.return_value = MagicMock(exit_code=0, stdout=privacy, stderr="")
    assert PopupBypasser(adb_client=mock_adb).detect_and_dismiss_popups() is True
    taps = [c for c in mock_adb.run_adb.call_args_list if c.args[:3] == ("shell", "input", "tap")]
    assert [c.args[3:] for c in taps] == [("540", "1470")]


def test_popup_bypass_denies_android_notification_permission_dialog():
    """抖音首次启动后的系统通知权限框：录制不需要通知，点“不允许”，不能误点“允许”。"""
    from src.worker.popup_bypasser import PopupBypasser

    mock_adb = MagicMock()
    dialog = """
    <hierarchy><node text="" package="com.android.permissioncontroller" bounds="[0,0][1080,1920]">
        <node text="要允许“抖音”向您发送通知吗？" package="com.android.permissioncontroller" bounds="[147,762][933,936]" />
        <node text="允许" package="com.android.permissioncontroller" bounds="[147,1014][933,1182]" />
        <node text="不允许" package="com.android.permissioncontroller" bounds="[147,1194][933,1362]" />
    </node></hierarchy>
    """
    mock_adb.run_adb.return_value = MagicMock(exit_code=0, stdout=dialog, stderr="")
    assert PopupBypasser(adb_client=mock_adb).detect_and_dismiss_popups() is True
    taps = [c for c in mock_adb.run_adb.call_args_list if c.args[:3] == ("shell", "input", "tap")]
    assert [c.args[3:] for c in taps] == [("540", "1278")]
