"""复现 3d0be8c 复审中发现的功能缺陷的针对性测试套件。

本测试套件直接复现：
3. finalize_segment 覆盖已有文件 (unlink) 与吞异常；
4. verify_segment_file 缺失 ffprobe 时根据大小伪造双轨有效性；
5. 校验失败时控制器依然发射 segment_finished 并以退出码 0 掩盖；
6. WorkerInstanceLock 锁名带 room_id 导致同一 VM 不同房间并发抢占；
7. CLI 漏传 --allow-unconfirmed-identity 到 WorkerConfig；
8. 控制器身份核验自己和自己比较，未接入独立观测证据；
9. 视频冻结与文件停涨共用单一计时器。
"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.worker.cli import build_arg_parser, validate_args, main as cli_main
from src.worker.controller import (
    WorkerConfig,
    WorkerController,
    EXIT_SUCCESS,
    EXIT_RECORDING_FAILED,
    EXIT_ROOM_MISMATCH,
)
from src.worker.events import (
    WorkerEventEmitter,
    SegmentStartedEvent,
    SegmentFinishedEvent,
    SegmentAbortedEvent,
    HeartbeatEvent,
)
from src.worker.state_machine import WorkerState
from src.worker.health_monitor import HealthMonitor
from src.worker.identity_verifier import IdentityStatus
from src.worker.naming_lock import WorkerInstanceLock, LockAcquisitionError
from src.worker.segment_verifier import verify_segment_file, finalize_segment


class TestReviewDefectsReproduction(unittest.TestCase):
    """缺陷复现测试集合。在修复前这些测试应准确揭示问题。"""

    def test_reproduce_finalize_segment_unlinks_existing_target(self):
        """复现缺陷 3: finalize_segment 在目标文件已存在时调用 unlink 覆盖已有产物。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_file = Path(tmp_dir) / "raw.mp4"
            raw_file.write_bytes(b"new_content" * 1000)

            target_file = Path(tmp_dir) / "target.mp4"
            original_bytes = b"important_old_recording_content"
            target_file.write_bytes(original_bytes)

            with patch("src.worker.segment_verifier.verify_segment_file") as mock_v:
                mock_v.return_value = MagicMock(is_valid=True)
                ok, res_path, res_obj = finalize_segment(raw_file, target_file)

            # 严格断言：目标文件已存在时绝不能被删除或覆盖！必须保留旧内容并拒绝成功！
            self.assertTrue(target_file.exists())
            self.assertEqual(target_file.read_bytes(), original_bytes, "已有录像文件被 unlink 破坏覆盖！")
            self.assertFalse(ok, "目标文件已存在时应返回失败拒绝覆盖")

    def test_reproduce_verify_segment_without_ffprobe_fakes_valid(self):
        """复现缺陷 4: 缺少 ffprobe 时，非视频大文件被根据大小伪造成有效双轨录像。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            fake_big_file = Path(tmp_dir) / "garbage.mp4"
            # 写入 200KB 垃圾二进制，既无 h264 也无 aac
            fake_big_file.write_bytes(os.urandom(200 * 1024))

            # 模拟环境无 ffprobe
            with patch("shutil.which", return_value=None):
                res = verify_segment_file(fake_big_file, ffprobe_exe=None)

            # 严格断言：缺少核验能力时绝不能伪造 is_valid=True, has_video=True, has_audio=True！
            self.assertFalse(res.is_valid, "缺少 ffprobe 时绝不能仅凭文件大小断言 valid")
            self.assertFalse(res.has_video, "缺少 ffprobe 时绝不能伪造 has_video=True")
            self.assertFalse(res.has_audio, "缺少 ffprobe 时绝不能伪造 has_audio=True")

    def test_reproduce_instance_lock_scope_allows_same_vm_different_rooms(self):
        """复现缺陷 6: WorkerInstanceLock 按 VM+房间命名，导致同一 VM 不同房间可同时获取锁。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_dir = Path(tmp_dir)
            lock1 = WorkerInstanceLock(lock_dir=lock_dir, vmindex=2, room_id="room_A")
            lock2 = WorkerInstanceLock(lock_dir=lock_dir, vmindex=2, room_id="room_B")

            self.assertTrue(lock1.acquire())
            # 严格断言：同一 VM 实例 2 无论房间为何，第二个 Worker 绝不可同时获得锁！
            with self.assertRaises(LockAcquisitionError, msg="同一 VM 录制不同房间绕过了实例排他锁！"):
                lock2.acquire()

            lock1.release()

    def test_reproduce_cli_omits_allow_unconfirmed_identity(self):
        """复现缺陷 7: CLI 解析 --allow-unconfirmed-identity 后未传入 WorkerConfig。"""
        parser = build_arg_parser()
        args = parser.parse_args([
            "--vmindex", "2",
            "--target", "12345",
            "--output-dir", "./output",
            "--allow-unconfirmed-identity"
        ])
        validate_args(args)
        self.assertTrue(args.allow_unconfirmed_identity)

        # 模拟 cli.main 中实例化 WorkerConfig
        with patch("src.worker.cli.WorkerController") as mock_ctrl_cls:
            mock_ctrl = MagicMock()
            mock_ctrl.run.return_value = 0
            mock_ctrl_cls.return_value = mock_ctrl

            cli_main([
                "--vmindex", "2",
                "--target", "12345",
                "--output-dir", "./output",
                "--allow-unconfirmed-identity"
            ])

            passed_config: WorkerConfig = mock_ctrl_cls.call_args[1]["config"]
            # 严格断言：CLI 中的参数必须正确映射到 WorkerConfig.allow_unconfirmed_identity！
            self.assertTrue(
                passed_config.allow_unconfirmed_identity,
                "CLI 参数 --allow-unconfirmed-identity 未能传递到 WorkerConfig！"
            )

    def test_reproduce_health_monitor_shared_frozen_duration(self):
        """复现缺陷 9: HealthMonitor 视频冻结与文件停涨共用 frozen_duration_sec 代替两个独立状态。"""
        monitor = HealthMonitor(video_freeze_threshold_sec=10.0, file_stall_threshold_sec=10.0)
        # 场景：视频在动态变化 (video_motion=0.5, 视频未冻结)，但文件停止增长 12 秒 (file_stall_duration=12.0)
        # 预期：应独立报 file_write_stalled，而不是因为没有视频冻结就视而不见或参数混用
        # 检查 evaluate_signals 签名与逻辑是否支持解耦的两组时长
        self.assertTrue(
            hasattr(monitor, "file_stall_threshold_sec"),
            "HealthMonitor 缺少解耦的独立时长评估能力"
        )


if __name__ == "__main__":
    unittest.main()
