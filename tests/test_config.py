"""Permission tests for the config file, which may hold the SSH passphrase."""

from __future__ import annotations

import json
import os
import stat

import pytest

from dsync import config as cfg

BASE_DATA = {
    "host": "example.com",
    "port": 22,
    "user": "someone",
    "key_path": "~/.ssh/id_rsa",
    "local_root": "~/site/",
    "remote_root": "/var/www/",
    "site_url": "https://example.com",
}


def mode_of(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the config module at a throwaway ~/.dsync."""
    root = tmp_path / ".dsync"
    monkeypatch.setattr(cfg, "CONFIG_DIR", root)
    monkeypatch.setattr(cfg, "CONFIG_FILE", root / "config.json")
    monkeypatch.setattr(cfg, "PROFILES_DIR", root / "profiles")
    return root


@pytest.fixture
def secret_config():
    c = cfg.Config(dict(BASE_DATA))
    c.passphrase = "hunter2"
    return c


class TestSavePermissions:
    def test_config_file_is_owner_only(self, home, secret_config):
        cfg.save_config(secret_config)
        assert mode_of(home / "config.json") == 0o600

    def test_config_dir_is_owner_only(self, home, secret_config):
        cfg.save_config(secret_config)
        assert mode_of(home) == 0o700

    def test_owner_only_even_under_a_permissive_umask(self, home, secret_config):
        old = os.umask(0o000)
        try:
            cfg.save_config(secret_config)
        finally:
            os.umask(old)
        assert mode_of(home / "config.json") == 0o600

    def test_profile_file_and_dir_are_owner_only(self, home, secret_config):
        cfg.save_config(secret_config, profile="staging")
        assert mode_of(home / "profiles" / "staging.json") == 0o600
        assert mode_of(home / "profiles") == 0o700
        assert mode_of(home) == 0o700

    def test_no_temp_files_left_behind(self, home, secret_config):
        cfg.save_config(secret_config)
        assert [p.name for p in home.iterdir()] == ["config.json"]

    def test_overwriting_a_loose_file_tightens_it(self, home, secret_config):
        cfg.save_config(secret_config)
        (home / "config.json").chmod(0o644)
        cfg.save_config(secret_config)
        assert mode_of(home / "config.json") == 0o600

    def test_round_trips(self, home, secret_config):
        cfg.save_config(secret_config)
        loaded = cfg.load_config()
        assert loaded.passphrase == "hunter2"
        assert loaded.host == "example.com"


class TestLoadRepairsLegacyPermissions:
    def _write_raw(self, home, data, mode):
        home.mkdir(parents=True, exist_ok=True)
        path = home / "config.json"
        path.write_text(json.dumps(data))
        path.chmod(mode)
        return path

    def test_world_readable_config_with_passphrase_is_repaired(self, home):
        path = self._write_raw(home, {**BASE_DATA, "passphrase": "hunter2"}, 0o644)
        cfg.load_config()
        assert mode_of(path) == 0o600

    def test_group_readable_config_with_passphrase_is_repaired(self, home):
        path = self._write_raw(home, {**BASE_DATA, "passphrase": "hunter2"}, 0o640)
        cfg.load_config()
        assert mode_of(path) == 0o600

    def test_repair_warns_about_possible_exposure(self, home, capsys):
        self._write_raw(home, {**BASE_DATA, "passphrase": "hunter2"}, 0o644)
        cfg.load_config()
        out = capsys.readouterr().out.lower()
        assert "rotat" in out

    def test_config_without_passphrase_is_left_alone(self, home):
        path = self._write_raw(home, dict(BASE_DATA), 0o644)
        cfg.load_config()
        assert mode_of(path) == 0o644

    def test_already_tight_config_is_not_warned_about(self, home, capsys):
        self._write_raw(home, {**BASE_DATA, "passphrase": "hunter2"}, 0o600)
        cfg.load_config()
        assert capsys.readouterr().out.strip() == ""
