"""在独立 Git 工作区检查已跟踪、未跟踪以及被忽略的文档。"""
import subprocess

from scripts.check_docs import check


def test_working_tree_links(tmp_path, capsys, monkeypatch):
    # 临时 Git 仓库无需宿主进程注入的命令行配置。
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / "说明.md"
    target.write_text("# 使用 方法\n## 使用 方法\n", encoding="utf8")
    subprocess.run(["git", "add", "--", target.name], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("忽略.md\n", encoding="utf8")
    (tmp_path / "忽略.md").write_text("[示例](不存在.md)", encoding="utf8")
    new = tmp_path / "未跟踪.md"
    new.write_text(
        "[原章节](说明.md#使用-方法-1)\n[旧标题](说明.md#已经改名)\n[已移动](旧文件.md)\n"
        "`[命令示例](不存在.md)`\n~~~md\n[围栏示例](不存在.md)\n~~~\n"
        "[外部项目](../other/README.md)\n", encoding="utf8",
    )
    assert check(tmp_path) == 1
    report = capsys.readouterr().out
    assert "标题锚点不存在 未跟踪.md:2" in report and "目标不存在 未跟踪.md:3" in report
    assert "仓内链接 3，错误 2，仓外未核验 1" in report
    new.write_text("[修正](说明.md#使用-方法)\n", encoding="utf8")
    assert check(tmp_path) == 0
    target.unlink()
    assert check(tmp_path) == 1
