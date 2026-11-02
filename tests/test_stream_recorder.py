"""StreamRecorder 模块单元测试。"""
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from src.mvp.stream_recorder import StreamRecorder


class TestStreamRecorder(unittest.TestCase):
    @patch("subprocess.Popen")
    @patch.object(Path, "exists", return_value=True)
    @patch.object(Path, "unlink", return_value=None)
    @patch.object(Path, "mkdir", return_value=None)
    @patch("time.sleep", return_value=None)
    def test_start_recording(self, mock_sleep, mock_mkdir, mock_unlink, mock_exists, mock_popen):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        recorder = StreamRecorder(
            serial="127.0.0.1:16384",
            output_path=Path("D:/temp/test.mp4"),
            scrcpy_exe=Path("D:/scrcpy/scrcpy.exe"),
            adb_exe=Path("D:/adb/adb.exe"),
        )
        recorder.start()
        self.assertTrue(recorder.is_recording())

        mock_proc.poll.return_value = 0
        recorder.stop()
        self.assertFalse(recorder.is_recording())


if __name__ == "__main__":
    unittest.main()
