"""验证 CLI 身份准入开关传递到实际配置。"""
import unittest
from unittest.mock import MagicMock, patch

from src.worker.cli import build_arg_parser, validate_args, main as cli_main
from src.worker.controller import WorkerConfig


class TestReviewDefectsReproduction(unittest.TestCase):
    """缺陷复现测试集合。在修复前这些测试应准确揭示问题。"""

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

if __name__ == "__main__":
    unittest.main()
