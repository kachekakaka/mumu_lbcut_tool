"""便携包首次选择、重开、丢库与拒写目录的离线回归。"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.portable import launcher


def test_cancel_then_reopen_from_another_bundle(tmp_path):
    home, recordings = tmp_path / "用户设置", tmp_path / "录 像"
    bundle, replacement, workspace = (tmp_path / name for name in ("便携包", "换包", "工 作区"))
    bundle.mkdir()
    replacement.mkdir()
    recordings.mkdir()
    with patch.object(launcher, "choose_workspace", return_value=None):
        assert launcher.workspace_for(bundle, home) is None
    assert not workspace.exists()
    assert not (bundle / "workspace.json").exists()
    with patch.object(launcher, "choose_workspace", return_value=workspace):
        chosen = launcher.workspace_for(bundle, home, recordings)
        assert chosen == launcher.workspace_for(replacement, home)
    assert chosen == (workspace / "runtime-console-unified", workspace / "upload-platform", recordings)
    pointer = replacement / "workspace.json"
    assert json.loads(pointer.read_text(encoding="utf-8")) == {"dataDirectory": str(workspace)}
    with patch.object(launcher, "choose_workspace", side_effect=AssertionError):
        assert launcher.workspace_for(replacement, home) == chosen


def test_existing_settings_never_replace_a_missing_database(tmp_path):
    home, bundle, workspace = (tmp_path / name for name in ("用户设置", "便携包", "工作区"))
    bundle.mkdir()
    with patch.object(launcher, "choose_workspace", return_value=workspace):
        data, _, _ = launcher.workspace_for(bundle, home)
    database = data / launcher.DB_NAME
    saved = database.with_suffix(".saved")
    database.rename(saved)
    with pytest.raises(FileNotFoundError, match="原调度数据库丢失"):
        launcher.workspace_for(bundle, home)
    assert saved.is_file() and not database.exists()


def test_denied_recordings_directory_reports_path_after_one_attempt(tmp_path):
    recordings = tmp_path / "只读录像盘"
    recordings.mkdir()
    with patch.object(Path, "open", side_effect=PermissionError) as create:
        with pytest.raises(OSError, match="无法在此目录写入") as failure:
            launcher.probe_directory(recordings, hardlink=True)
    create.assert_called_once_with("xb")
    assert str(recordings) in str(failure.value)


def test_offline_corrupt_and_overlapping_directories(tmp_path):
    home, bundle, workspace = (tmp_path / name for name in ("用户设置", "便携包", "工作区"))
    bundle.mkdir()
    with patch.object(launcher, "choose_workspace", return_value=bundle):
        with pytest.raises(ValueError, match="重叠"):
            launcher.workspace_for(bundle, home)
    with patch.object(launcher, "choose_workspace", return_value=workspace):
        _, _, recordings = launcher.workspace_for(bundle, home)
    recordings.rename(workspace / "已断开的录像")
    with pytest.raises(FileNotFoundError, match="录像目录不可用"):
        launcher.workspace_for(bundle, home)
    pointer = bundle / "workspace.json"
    pointer.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="workspace.json"):
        launcher.workspace_for(bundle, home)
