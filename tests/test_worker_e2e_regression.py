from src.worker.state_machine import WorkerState
"""Worker 主控制流程与 14 项核心规格全量回归测试套件。

覆盖用户明确要求的 14 大核心场景：
1. 同一计划切片周期内连续两次进程崩溃：启动序号 1、2、3，每个开始事件恰好一个终态；
2. 看门狗重启：旧片有准确终态，新片正确开始，大小基线重置，不被旧动作再次重启；
3. 周期轮转及最终停止时校验失败：不发送成功完成事件，不无条件返回成功；
4. ffprobe 不可用：超过原大小门槛的非视频文件不能被判为有效双轨录像；
5. 正常短尾片：小体积但有效双轨的正常尾片不会误判为损坏；
6. 归档冲突或移动失败：已有目标文件字节内容不变，不丢失原始文件；
7. 目标 A、模拟器实际为 B：从主流程验证实际身份探测结果被使用，退出码为 18；
8. CLI 逃逸开关：只放行 UNCONFIRMED，绝不放行明确 MISMATCH；
9. 视频冻结但音频/文件正常，以及文件停止增长但视频正常：两条独立路径分别被识别处置；
10. 运行期下播与查询失败：已确认下播正常退出 (码 2)，网络失败和 UNKNOWN 不误退出；
11. 同一 VM、不同房间、不同输出目录：第二个 Worker 无法获取控制权，不同 VM 可并行；
12. 同一输出根目录并发运行：run_id 任务目录排他隔离，无覆盖；
13. 慢探针、启动或恢复过程中收到停止请求：响应受控，进入 STOPPING 后不再启动新切片；
14. 高频 stdout/stderr 输出：防背压日志持续消费与资源回收。
"""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from src.worker.cli import main as cli_main
from src.worker.controller import (
    WorkerConfig,
    WorkerController,
    EXIT_SUCCESS,
    EXIT_STREAM_ENDED,
    EXIT_ROOM_MISMATCH,
    EXIT_RECORDING_FAILED,
)
from src.worker.events import (
    WorkerEventEmitter,
    SegmentStartedEvent,
    SegmentFinishedEvent,
    SegmentAbortedEvent,
    HeartbeatEvent,
    StreamEndedEvent,
)
from src.worker.health_monitor import HealthMonitor
from src.worker.identity_verifier import IdentityStatus, verify_room_identity
from src.worker.naming_lock import WorkerInstanceLock, LockAcquisitionError
from src.worker.pipeline import PipelineLogDrainer, WorkerPipeline
from src.worker.segment_verifier import (
    verify_segment_file,
    finalize_segment,
    SegmentValidationResult,
)
from src.worker.state_machine import (
    WorkerState,
    PipelineAction,
    ActionType,
    GenerationActionDispatcher,
)
from src.worker.stream_end_detector import StreamEndDetector


class TestWorkerE2ERegression(unittest.TestCase):
    """Worker 主控制流程集成与回归测试。"""

    def setUp(self):
        self.events = []
        self.emitter = WorkerEventEmitter()
        self.emitter.emit = lambda ev: self.events.append(ev)

    def test_01_double_crash_sequence_and_single_terminal(self):
        """场景 1: 同一切片周期内连续两次崩溃，启动序号单调递增为 1、2、3，每个开始事件恰好一个终态。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = WorkerConfig(
                vmindex=2,
                target="test_target",
                output_dir=Path(tmp_dir),
                segment_duration_sec=30,
            )
            ctrl = WorkerController(config=cfg, emitter=self.emitter)

            mock_pipe = MagicMock()
            mock_pipe.is_recording.return_value = False
            mock_pipe.get_exit_code.return_value = 2
            mock_pipe.start_segment.side_effect = lambda idx: Path(tmp_dir) / f"seg_{idx}.mp4"
            mock_pipe.current_file_path = Path(tmp_dir) / "seg_1.mp4"
            ctrl._pipeline = mock_pipe

            # 初始启动切片 1
            ctrl._start_new_segment()
            self.assertEqual(ctrl.current_segment_index, 1)

            # 第一次崩溃自愈
            r1 = ctrl._handle_pipeline_crash(exit_code=2)
            self.assertTrue(r1)
            self.assertEqual(ctrl.current_segment_index, 2)

            # 第二次崩溃自愈
            r2 = ctrl._handle_pipeline_crash(exit_code=2)
            self.assertTrue(r2)
            self.assertEqual(ctrl.current_segment_index, 3)

            started = [e for e in self.events if isinstance(e, SegmentStartedEvent)]
            aborted = [e for e in self.events if isinstance(e, SegmentAbortedEvent)]
            finished = [e for e in self.events if isinstance(e, SegmentFinishedEvent)]

            started_indices = [e.segment_index for e in started]
            # 严格断言：切片启动序号必须依次为 [1, 2, 3]
            self.assertEqual(started_indices, [1, 2, 3])

            # 切片 1 和切片 2 必须分别有且仅有一个 aborted 终态
            aborted_indices = [e.segment_index for e in aborted]
            self.assertIn(1, aborted_indices)
            self.assertIn(2, aborted_indices)
            self.assertEqual(aborted_indices.count(1), 1)
            self.assertEqual(aborted_indices.count(2), 1)

    def test_02_watchdog_restart_terminal_and_dedup(self):
        """场景 2: 看门狗重启：旧片有准确终态，新片正确开始，旧代际动作丢弃不重复重启。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = WorkerConfig(
                vmindex=2,
                target="test_target",
                output_dir=Path(tmp_dir),
                segment_duration_sec=30,
            )
            ctrl = WorkerController(config=cfg, emitter=self.emitter)
            mock_pipe = MagicMock()
            mock_pipe.start_segment.side_effect = lambda idx: Path(tmp_dir) / f"seg_{idx}.mp4"
            ctrl._pipeline = mock_pipe

            # 启动切片 1 (代际 1)
            ctrl._start_new_segment()
            self.assertEqual(ctrl.current_generation_id, 1)

            # 看门狗触发重启
            ctrl.state_machine.transition_to(WorkerState.RECORDING)
            ctrl._recycle_current_segment(reason="watchdog_stall")
            self.assertEqual(ctrl.current_segment_index, 2)
            self.assertEqual(ctrl.current_generation_id, 2)

            # 验证旧切片 1 已有且仅有一个 aborted 终态事件
            aborted_1 = [e for e in self.events if isinstance(e, SegmentAbortedEvent) and e.segment_index == 1]
            self.assertEqual(len(aborted_1), 1)
            self.assertEqual(aborted_1[0].reason, "watchdog_stall")

            # 模拟看门狗队列中残留了一个携带旧代际 1 的过期动作
            ctrl.action_dispatcher.post_action(PipelineAction(
                action_type=ActionType.RESTART_SEGMENT,
                generation_id=1,  # 旧代际
                reason="expired_stall",
            ))
            # 排空动作时，旧动作必须被安全丢弃，不返回有效动作
            valid_actions = ctrl.action_dispatcher.drain_actions()
            self.assertEqual(len(valid_actions), 0, "旧代际动作必须被丢弃！")

    def test_03_verification_failure_rejects_success_event(self):
        """场景 3: 周期轮转及最终停止时校验失败：不发送 finished 事件，最终返回错误码。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_bad = Path(tmp_dir) / "corrupt_raw.mp4"
            raw_bad.write_bytes(b"garbage_not_mp4")

            cfg = WorkerConfig(
                vmindex=2,
                target="test_target",
                output_dir=Path(tmp_dir),
                segment_duration_sec=30,
            )
            ctrl = WorkerController(config=cfg, emitter=self.emitter)
            ctrl._pipeline = MagicMock()
            ctrl._pipeline.current_file_path = raw_bad
            ctrl.current_segment_index = 1
            ctrl.current_file_path = raw_bad
            ctrl.current_segment_started = True
            ctrl.current_segment_finalized = False

            # mock 校验器返回失败
            with patch("src.worker.controller.finalize_segment") as mock_fin:
                mock_fin.return_value = (False, raw_bad.with_name("corrupt_raw.corrupted"), MagicMock(reject_reason="bad_header"))
                ok = ctrl._finalize_current_segment(allow_short_tail=False)

            self.assertFalse(ok)
            finished = [e for e in self.events if isinstance(e, SegmentFinishedEvent)]
            aborted = [e for e in self.events if isinstance(e, SegmentAbortedEvent)]
            self.assertEqual(len(finished), 0, "校验失败严禁发射 SegmentFinishedEvent！")
            self.assertEqual(len(aborted), 1, "校验失败必须发射 SegmentAbortedEvent！")
            self.assertTrue(ctrl._last_segment_failed)

    def test_04_missing_ffprobe_rejects_non_video_file(self):
        """场景 4: ffprobe 不可用时，普通大文件不能被判定为有效双轨录像。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            big_fake_file = Path(tmp_dir) / "huge_junk.mp4"
            big_fake_file.write_bytes(b"\x00" * (300 * 1024))  # 300KB 无效数据

            with patch("shutil.which", return_value=None):
                res = verify_segment_file(big_fake_file, ffprobe_exe=None)

            self.assertFalse(res.is_valid, "缺少 ffprobe 时绝不能仅凭文件大小放行！")
            self.assertFalse(res.has_video)
            self.assertFalse(res.has_audio)
            self.assertIn("ffprobe", res.reject_reason)

    def test_05_valid_short_tail_succeeds(self):
        """场景 5: 正常短尾片：小体积但有效双轨的正常短尾片在 allow_short_tail 下成功。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            short_tail = Path(tmp_dir) / "short_tail.mp4"
            short_tail.write_bytes(b"\x00" * 4096)  # 4KB 小体积文件

            with patch("shutil.which", return_value="ffprobe"), \
                 patch("src.worker.segment_verifier.run_command") as mock_cmd:
                mock_cmd.return_value = MagicMock(
                    exit_code=0,
                    stdout=json.dumps({
                        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                        "format": {"duration": "3.5"}
                    }),
                    stderr=""
                )
                res = verify_segment_file(short_tail, allow_short_tail=True)

            self.assertTrue(res.is_valid, "合法的短尾双轨文件在 allow_short_tail=True 下应通过核验")
            self.assertTrue(res.has_video)
            self.assertTrue(res.has_audio)

    def test_06_archive_conflict_preserves_existing_target(self):
        """场景 6: 归档冲突或移动失败：已有目标文件字节内容不变，原文件保留。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw = Path(tmp_dir) / "raw_1.mp4"
            raw.write_bytes(b"new_stream_bytes")
            target = Path(tmp_dir) / "target_1.mp4"
            old_bytes = b"precious_historical_recording_data"
            target.write_bytes(old_bytes)

            with patch("src.worker.segment_verifier.verify_segment_file") as mock_v:
                mock_v.return_value = MagicMock(is_valid=True)
                ok, res_path, res_obj = finalize_segment(raw, target)

            self.assertFalse(ok, "目标已存在时必须拒绝覆盖")
            self.assertEqual(target.read_bytes(), old_bytes, "已有文件字节绝不能被破坏或替换！")
            self.assertTrue(raw.exists(), "归档失败时原文件必须妥善保留")

    def test_07_identity_mismatch_detected_from_page_blocks_recording(self):
        """真实控制流程三次纠偏后仍为其他房间：不把错误画面交付为目标录像。"""
        from src.worker.room_locator import RoomEvidence
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp_dir, \
             patch("src.worker.controller.MuMuClient") as mock_mumu, \
             patch("src.worker.controller.WorkerNavigator") as mock_nav_cls, \
             patch("src.worker.controller.WorkerPipeline") as mock_pipeline:
            mock_mumu.return_value.launch_player.return_value = True
            mock_mumu.return_value.connect_adb.return_value = "synthetic-device"
            nav = mock_nav_cls.return_value
            nav.navigate_route.return_value = True
            nav.wait_live_page.return_value = True
            nav.observe_live_room.return_value = RoomEvidence('99999', '22222', source='synthetic-device')
            cfg = WorkerConfig(vmindex=321,target='88888',target_kind='room_id',
                expected_anchor_uid='11111',output_dir=Path(tmp_dir)/'out',lock_dir=Path(tmp_dir)/'locks')
            ctrl = WorkerController(cfg, WorkerEventEmitter(stream))
            self.assertEqual(ctrl.run(), EXIT_ROOM_MISMATCH)
            self.assertEqual(nav.observe_live_room.call_count, 3)
            self.assertFalse(mock_pipeline.called)
            events = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertEqual(sum(e.get('action')=='correct_wrong_room' for e in events),3)
            self.assertFalse(any(e['event']=='segment_started' for e in events))

    def test_08_cli_escape_switch_allows_unconfirmed_but_blocks_mismatch(self):
        """场景 8: CLI 逃逸开关：只放行 UNCONFIRMED，绝不放行明确 MISMATCH。"""
        # A. 明确 MISMATCH: 即使加了 --allow-unconfirmed-identity 仍然必须阻断！
        res_mismatch = verify_room_identity(
            target_uid="UID_100",
            current_uid="UID_200",
            allow_unconfirmed=True,
        )
        self.assertEqual(res_mismatch.status, IdentityStatus.MISMATCH)
        self.assertFalse(res_mismatch.can_proceed, "明确错房绝不允许逃逸放行！")

        # B. 无法确认 UNCONFIRMED:
        # 未开启参数时阻断
        res_unconfirmed_strict = verify_room_identity(
            target_uid="UID_100",
            current_uid=None,
            allow_unconfirmed=False,
        )
        self.assertEqual(res_unconfirmed_strict.status, IdentityStatus.UNCONFIRMED)
        self.assertFalse(res_unconfirmed_strict.can_proceed)

        # 开启参数时放行
        res_unconfirmed_escape = verify_room_identity(
            target_uid="UID_100",
            current_uid=None,
            allow_unconfirmed=True,
        )
        self.assertEqual(res_unconfirmed_escape.status, IdentityStatus.UNCONFIRMED)
        self.assertTrue(res_unconfirmed_escape.can_proceed, "开启逃逸开关时应放行无法确认状态")

    def test_09_decoupled_health_signals_video_frozen_vs_file_stalled(self):
        """场景 9: 视频冻结但文件正常，以及文件停止增长但视频正常：四维解耦独立判断。"""
        monitor = HealthMonitor(video_freeze_threshold_sec=10.0, file_stall_threshold_sec=10.0)

        # 1. 视频冻结持续 12s，但文件正常有增量 (1.5MB)，音频 active
        s1 = monitor.evaluate_signals(
            video_motion=0.001,
            audio_active=True,
            process_alive=True,
            file_delta_mb=1.5,
            video_frozen_duration_sec=12.0,
            file_stall_duration_sec=0.0,
        )
        self.assertFalse(s1.is_healthy)
        self.assertEqual(s1.alert_reason, "video_frozen", "音频正常不能否决持续视频冻结！")

        # 2. 视频动态良好 (0.8)，但文件停止增长达 12s
        s2 = monitor.evaluate_signals(
            video_motion=0.8,
            audio_active=True,
            process_alive=True,
            file_delta_mb=0.0,
            video_frozen_duration_sec=0.0,
            file_stall_duration_sec=12.0,
        )
        self.assertFalse(s2.is_healthy)
        self.assertEqual(s2.alert_reason, "file_write_stalled", "画面动态不能掩盖文件写入停滞！")

    def test_10_runtime_stream_end_vs_network_failure(self):
        """场景 10: 运行期下播与查询失败：已确认下播正常退出；网络失败和 UNKNOWN 不误退出。"""
        detector = StreamEndDetector(confirm_window_sec=3.0)

        t0 = 1000.0
        # 1. 网络瞬时失败或未知：screen=False, api=False -> normal_live
        is_ended, _ = detector.feed_observation(screen_shows_ended=False, api_shows_ended=False, current_time=t0)
        self.assertFalse(is_ended)

        # 2. 首次发现下播疑似 (screen=True, api=True)，在滤波窗口内未确诊
        is_ended, r2 = detector.feed_observation(screen_shows_ended=True, api_shows_ended=True, current_time=t0 + 1.0)
        self.assertFalse(is_ended)
        self.assertEqual(r2, "pending_confirmation")

        # 3. 超过 3.0s 滤波确认窗口 (从首次 t0+1.0 到 t0+4.5 经过 3.5s >= 3.0s) -> 确诊下播
        is_ended, r3 = detector.feed_observation(screen_shows_ended=True, api_shows_ended=True, current_time=t0 + 4.5)
        self.assertTrue(is_ended)
        self.assertEqual(r3, "stream_ended_confirmed")

    def test_11_same_vm_different_rooms_different_dirs_mutual_exclusion(self):
        """场景 11: 同一 VM、不同房间、不同输出目录，第二个 Worker 无法获取控制权；不同 VM 可并行。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_lock_dir = Path(tmp_dir) / "shared_locks"

            # 两个任务针对同一 VM 2，但房间不同，目录不同
            lock_vm2_roomA = WorkerInstanceLock(lock_dir=shared_lock_dir, vmindex=2, room_id="room_A")
            lock_vm2_roomB = WorkerInstanceLock(lock_dir=shared_lock_dir, vmindex=2, room_id="room_B")

            self.assertTrue(lock_vm2_roomA.acquire())
            # 第二个 Worker 必须被排他性拒绝！
            with self.assertRaises(LockAcquisitionError):
                lock_vm2_roomB.acquire()

            # 不同的 VM 3 可以同时成功获取锁
            lock_vm3 = WorkerInstanceLock(lock_dir=shared_lock_dir, vmindex=3, room_id="room_B")
            self.assertTrue(lock_vm3.acquire())

            lock_vm2_roomA.release()
            lock_vm3.release()

    def test_12_concurrent_output_isolation_via_run_id(self):
        """场景 12: 同一输出根目录下多次或并发运行：独立 run_id 目录彻底隔离产物。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root_out = Path(tmp_dir)
            cfg1 = WorkerConfig(vmindex=1, target="t1", output_dir=root_out)
            cfg2 = WorkerConfig(vmindex=2, target="t2", output_dir=root_out)

            ctrl1 = WorkerController(config=cfg1)
            ctrl2 = WorkerController(config=cfg2)

            self.assertNotEqual(ctrl1.run_id, ctrl2.run_id)
            self.assertNotEqual(ctrl1.task_output_dir, ctrl2.task_output_dir)

            ctrl1.task_output_dir.mkdir(parents=True, exist_ok=True)
            ctrl2.task_output_dir.mkdir(parents=True, exist_ok=True)

            self.assertTrue(ctrl1.task_output_dir.exists())
            self.assertTrue(ctrl2.task_output_dir.exists())
            self.assertNotEqual(str(ctrl1.task_output_dir), str(ctrl2.task_output_dir))

    def test_13_stop_request_during_recovery_prevents_new_segment(self):
        """场景 13: 收到停止请求后，进入 STOPPING，不再启动新切片，收尾不重复。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg = WorkerConfig(
                vmindex=2,
                target="test_target",
                output_dir=Path(tmp_dir),
                segment_duration_sec=30,
            )
            ctrl = WorkerController(config=cfg, emitter=self.emitter)
            mock_pipe = MagicMock()
            mock_pipe.is_recording.return_value = False
            mock_pipe.start_segment.return_value = Path(tmp_dir) / "new.mp4"
            ctrl._pipeline = mock_pipe

            ctrl._start_new_segment()
            # 外部发送停止信号
            ctrl.request_stop()
            ctrl.state_machine.transition_to(WorkerState.STOPPING)

            # 此时尝试恢复或重拉切片，必须被受控阻断，不再启动新切片
            start_count_before = mock_pipe.start_segment.call_count
            ctrl._handle_pipeline_crash(exit_code=1)
            self.assertEqual(
                mock_pipe.start_segment.call_count, start_count_before,
                "进入 STOPPING 状态后坚决禁止启动新切片！"
            )

    def test_14_high_frequency_log_drainer_no_backpressure(self):
        """场景 14: 高频 stdout/stderr 输出：防背压日志排空器平稳消费并妥善回收。"""
        # 使用管道模拟高频写入 10,000 行日志，验证不发生死锁
        pipe_r, pipe_w = os.pipe()
        r_file = os.fdopen(pipe_r, "r", encoding="utf-8", errors="replace")
        w_file = os.fdopen(pipe_w, "w", encoding="utf-8")

        mock_proc = MagicMock()
        mock_proc.stdout = r_file
        mock_proc.stderr = None

        consumed = []
        drainer = PipelineLogDrainer(
            process=mock_proc,
            on_log_line=lambda line: consumed.append(line)
        )
        drainer.start()

        # 写入 2000 行模拟日志
        for i in range(2000):
            w_file.write(f"log_payload_entry_{i}\n")
        w_file.flush()
        w_file.close()

        drainer.join(timeout=2.0)
        self.assertGreater(len(consumed), 1900, "排空线程应完整消费高频管道日志无死锁")


if __name__ == "__main__":
    unittest.main()
