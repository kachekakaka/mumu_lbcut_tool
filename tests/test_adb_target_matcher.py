"""测试 ADB 严格精确匹配与端口就绪轮询 (Ticket 04)。"""
from unittest.mock import MagicMock, patch
import pytest

from src.worker.adb_client import (
    AdbDeviceClient,
    DeviceUnavailableError,
)


def test_connect_adb_strictly_rejects_other_devices():
    """目标设备离线，仅存在其他在线设备时，坚决报错，绝不串绑到非目标设备。"""
    # 模拟 adb devices 输出：目标是 127.0.0.1:16448，但当前仅有 127.0.0.1:16480 在线
    mock_devices_output = (
        "List of devices attached\n"
        "127.0.0.1:16480\tdevice\n"
    )

    client = AdbDeviceClient(serial="127.0.0.1:16448")

    with patch("src.worker.adb_client.run_command") as mock_run:
        # 模拟 connect 与 devices 命令的返回
        mock_run.return_value = MagicMock(exit_code=0, stdout=mock_devices_output, stderr="")
        with pytest.raises(DeviceUnavailableError) as exc_info:
            client.wait_for_device(timeout_sec=0.5, poll_interval=0.1)
        
        assert "127.0.0.1:16448" in str(exc_info.value)
        assert client.current_serial == "127.0.0.1:16448"


def test_adb_commands_always_include_serial():
    """所有由 AdbDeviceClient 发送的 adb 命令必须严格携带 -s <exact_serial>。"""
    client = AdbDeviceClient(serial="127.0.0.1:16384")

    with patch("src.worker.adb_client.run_command") as mock_run:
        mock_run.return_value = MagicMock(exit_code=0, stdout="device\n", stderr="")
        client.run_adb("shell", "getprop", "ro.build.version.release")

        call_args = mock_run.call_args[0][0]
        assert "-s" in call_args
        s_index = call_args.index("-s")
        assert call_args[s_index + 1] == "127.0.0.1:16384"



def test_adb_client_reports_command_timing_by_category_and_can_reset():
    client = AdbDeviceClient(serial="127.0.0.1:16448")
    with patch("src.worker.adb_client.run_command") as mock_run:
        mock_run.return_value = MagicMock(exit_code=0, stdout="ok", stderr="")
        client.run_adb("shell", "dumpsys", "activity", "activities")
        client.run_adb("shell", "input", "tap", "10", "20")
    snapshot = client.metrics_snapshot()
    assert snapshot["commands"]["activity_probe"]["count"] == 1
    assert snapshot["commands"]["tap"]["count"] == 1
    assert snapshot["commands"]["activity_probe"]["total_ms"] >= 0
    client.reset_metrics()
    assert client.metrics_snapshot() == {"commands": {}, "slow_commands": []}
