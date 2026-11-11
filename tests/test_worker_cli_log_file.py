"""Worker --log-file：运行日志另存文件，结束后移除处理器。"""
import logging
from unittest.mock import patch

from src.worker.cli import main as cli_main


def test_log_file_receives_worker_logs_and_handler_is_removed(tmp_path, caplog):
    # 真实进程由 cli 模块的 basicConfig 设为 INFO；pytest 已占用根记录器，需显式设级别。
    caplog.set_level(logging.INFO)
    log_path = tmp_path / 'events' / 'job.log'
    root = logging.getLogger()
    before = list(root.handlers)
    def run():
        logging.getLogger('src.worker.controller').info('切片封口完成 seg_001')
        return 0
    with patch('src.worker.cli.WorkerController') as ctrl_cls:
        ctrl_cls.return_value.run.side_effect = run
        assert cli_main(['--vmindex', '2', '--target', '12345', '--output-dir', str(tmp_path / 'out'),
                         '--log-file', str(log_path)]) == 0
    assert root.handlers == before
    content = log_path.read_text(encoding='utf-8')
    assert '[INFO] (Worker) 切片封口完成 seg_001' in content
