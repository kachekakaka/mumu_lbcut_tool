"""便携包首次选择、重开、丢库与拒写目录的离线回归。"""
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.portable import launcher


def test_cancel_then_reopen_from_another_bundle(tmp_path):
    home, recordings = tmp_path / "用户设置", tmp_path / "录 像"
    home.mkdir()
    recordings.mkdir()
    with patch.object(launcher, "choose_recordings", return_value=None):
        assert launcher.recordings_for(home) is None
    assert not (home / "data").exists()
    with patch.object(launcher, "choose_recordings", return_value=recordings):
        assert launcher.recordings_for(home) == recordings
    with (patch.object(launcher, "BUNDLE", tmp_path / "另一处解压目录"),
          patch.object(launcher, "choose_recordings", side_effect=AssertionError)):
        assert launcher.recordings_for(home) == recordings


def test_existing_settings_never_replace_a_missing_database(tmp_path):
    home = tmp_path / "用户设置"
    home.mkdir()
    recordings = tmp_path / "录像"
    recordings.mkdir()
    with patch.object(launcher, "choose_recordings", return_value=recordings):
        launcher.recordings_for(home)
    database = home / "data" / launcher.DB_NAME
    saved = database.with_suffix(".saved")
    database.rename(saved)
    with pytest.raises(FileNotFoundError, match="原调度数据库丢失"):
        launcher.recordings_for(home)
    assert saved.is_file() and not database.exists()


def test_denied_recordings_directory_reports_path_after_one_attempt(tmp_path):
    home, recordings = tmp_path / "设置", tmp_path / "只读录像盘"
    recordings.mkdir()
    with patch.object(Path, "open", side_effect=PermissionError) as create:
        with pytest.raises(OSError, match="无法写入录像目录") as failure:
            launcher.writable_recordings(recordings, home)
    create.assert_called_once_with("xb")
    assert str(recordings) in str(failure.value)
