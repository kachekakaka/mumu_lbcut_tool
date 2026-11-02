"""配置模块单元测试。"""
import unittest
from src.mvp.config import REPO_ROOT, TMP_DIR, get_mumu_manager_exe, get_adb_exe


class TestConfig(unittest.TestCase):
    def test_paths_relative_to_repo(self):
        self.assertTrue(REPO_ROOT.exists())
        self.assertTrue((REPO_ROOT / "src").exists())
        self.assertEqual(TMP_DIR.name, "mumu_lbcut_tool_tmp")

    def test_mumu_executables_found(self):
        manager = get_mumu_manager_exe()
        self.assertIsNotNone(manager)
        self.assertTrue(manager.exists())
        self.assertEqual(manager.name.lower(), "mumumanager.exe")

        adb = get_adb_exe()
        self.assertIsNotNone(adb)
        self.assertTrue(adb.exists())
        self.assertEqual(adb.name.lower(), "adb.exe")


if __name__ == "__main__":
    unittest.main()
