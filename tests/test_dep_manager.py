"""依赖管理模块单元测试。"""
import unittest
from pathlib import Path
from src.mvp.dep_manager import DependencyManager
from src.mvp.config import SCRCPY_DIR, get_adb_exe


class TestDependencyManager(unittest.TestCase):
    def test_adb_ready(self):
        manager = DependencyManager()
        adb_path = manager.get_adb_path()
        self.assertTrue(adb_path.exists())
        self.assertEqual(adb_path.name.lower(), "adb.exe")

    def test_scrcpy_path_getter(self):
        manager = DependencyManager()
        expected = SCRCPY_DIR / "scrcpy.exe"
        self.assertEqual(manager.scrcpy_exe_path, expected)


if __name__ == "__main__":
    unittest.main()
