"""Worker 切片录制管道单元测试。"""
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from src.worker.pipeline import WorkerPipeline


class TestWorkerPipeline(unittest.TestCase):
    @patch("subprocess.Popen")
    @patch.object(Path, "exists", return_value=True)
    @patch.object(Path, "mkdir", return_value=None)
    @patch("time.sleep", return_value=None)
    def test_segment_lifecycle(self, mock_sleep, mock_mkdir, mock_exists, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        pipe = WorkerPipeline(
            serial="127.0.0.1:16448",
            output_dir=Path("D:/temp_output"),
            scrcpy_exe=Path("D:/scrcpy/scrcpy.exe"),
            adb_exe=Path("D:/adb/adb.exe"),
        )

        # 启动第 1 个切片
        file1 = pipe.start_segment(1)
        self.assertTrue(pipe.is_recording())
        self.assertTrue(str(file1).endswith("seg_001.mp4"))

        # 停止并封口第 1 个切片
        mock_proc.poll.return_value = 0
        ok = pipe.stop_segment()
        self.assertFalse(pipe.is_recording())


if __name__ == "__main__":
    unittest.main()
