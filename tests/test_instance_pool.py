"""测试模拟器实例池管理 (InstancePool)。"""
import unittest
from unittest.mock import MagicMock

from src.orchestrator.instance_pool import InstancePool, InstanceStatus, EXIT_LOGIN_BLOCKED


class TestInstancePool(unittest.TestCase):
    def test_instance_pool_refresh_and_acquire_release(self):
        mock_mumu = MagicMock()
        mock_mumu.list_available_instances.return_value = [
            {"vmindex": 2, "name": "lb1", "is_running": False},
            {"vmindex": 3, "name": "lb2", "is_running": False},
        ]

        pool = InstancePool(mumu_client=mock_mumu)
        slots = pool.refresh()
        self.assertEqual(len(slots), 2)

        # 申请第一个空闲实例
        slot1 = pool.acquire(target="room_A", job_id="job_001")
        self.assertIsNotNone(slot1)
        self.assertEqual(slot1.vmindex, 2)
        self.assertEqual(slot1.status, InstanceStatus.BUSY)
        self.assertEqual(slot1.current_target, "room_A")

        # 申请第二个空闲实例
        slot2 = pool.acquire(target="room_B", job_id="job_002")
        self.assertIsNotNone(slot2)
        self.assertEqual(slot2.vmindex, 3)

        # 无多余实例，申请返回 None
        slot3 = pool.acquire(target="room_C")
        self.assertIsNone(slot3)

        # 释放实例 2
        pool.release(vmindex=2)
        slot1_again = pool.get_slot(2)
        self.assertEqual(slot1_again.status, InstanceStatus.IDLE)
        self.assertIsNone(slot1_again.current_target)

    def test_instance_pool_auto_block_on_login_blocked(self):
        mock_mumu = MagicMock()
        mock_mumu.list_available_instances.return_value = [
            {"vmindex": 2, "name": "lb1", "is_running": False},
        ]
        pool = InstancePool(mumu_client=mock_mumu)
        pool.refresh()

        slot = pool.acquire("room_A")
        self.assertEqual(slot.vmindex, 2)

        # 当 Worker 以退出码 15 (EXIT_LOGIN_BLOCKED) 退出时，归还实例并触发自动隔离
        pool.release(vmindex=2, error_code=EXIT_LOGIN_BLOCKED)
        slot_blocked = pool.get_slot(2)
        self.assertEqual(slot_blocked.status, InstanceStatus.BLOCKED)
        self.assertIsNotNone(slot_blocked.blocked_reason)

        # 再次申请时，被隔离的实例不得分配给新任务
        new_slot = pool.acquire("room_B")
        self.assertIsNone(new_slot)

        # 人工干预后解除隔离
        unblock_ok = pool.unblock(2)
        self.assertTrue(unblock_ok)
        slot_ready = pool.get_slot(2)
        self.assertEqual(slot_ready.status, InstanceStatus.IDLE)


if __name__ == "__main__":
    unittest.main()
