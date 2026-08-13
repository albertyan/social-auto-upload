"""
sau_agent_pkg.updater 轻量单测：版本比较与 upgrade_notice 校验。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from sau_agent_pkg import updater


# ---------------------------------------------------------------------------
# 版本解析与比较
# ---------------------------------------------------------------------------
class TestVersionCompare:
    def test_parse_semver(self):
        assert updater.parse_version("1.2.0") == (1, 2, 0)
        assert updater.parse_version("10.0.1") == (10, 0, 1)

    def test_parse_with_suffix(self):
        assert updater.parse_version("1.2.0-beta") == (1, 2, 0)

    def test_parse_invalid(self):
        assert updater.parse_version("") is None
        assert updater.parse_version("abc") is None
        assert updater.parse_version(".1.2") is None
        assert updater.parse_version(None) is None  # type: ignore[arg-type]

    def test_is_newer(self):
        assert updater.is_newer_version("1.0.1", "1.0.0")
        assert updater.is_newer_version("1.2.0", "1.0.0")
        assert updater.is_newer_version("1.10.0", "1.9.9")
        assert updater.is_newer_version("2.0.0", "1.99.99")

    def test_not_newer(self):
        assert not updater.is_newer_version("1.0.0", "1.0.0")
        assert not updater.is_newer_version("0.9.9", "1.0.0")
        assert not updater.is_newer_version("abc", "1.0.0")


# ---------------------------------------------------------------------------
# validate_notice
# ---------------------------------------------------------------------------
def _notice(**overrides):
    data = {
        "version": "9.9.9",
        "download_url": "https://opc.example.com/pkg/sau-9.9.9.exe",
        "file_hash": "a" * 64,
    }
    data.update(overrides)
    return data


@pytest.fixture()
def mock_config():
    cfg = {"server_url": "wss://opc.example.com/opcgeo/agent/ws"}
    with patch.object(updater, "load_config", return_value=cfg):
        yield cfg


class TestValidateNotice:
    def test_valid(self, mock_config):
        ok, reason = updater.validate_notice(_notice())
        assert ok, reason

    def test_missing_field(self, mock_config):
        for key in ("version", "download_url", "file_hash"):
            ok, reason = updater.validate_notice(_notice(**{key: ""}))
            assert not ok
            assert "missing" in reason

    def test_bad_hash(self, mock_config):
        ok, reason = updater.validate_notice(_notice(file_hash="xyz"))
        assert not ok
        assert "sha256" in reason

    def test_bad_version(self, mock_config):
        ok, _ = updater.validate_notice(_notice(version="not-a-version"))
        assert not ok

    def test_version_not_newer(self, mock_config):
        with patch.object(updater, "APP_VERSION", "2.0.0"):
            ok, reason = updater.validate_notice(_notice(version="1.9.0"))
            assert not ok
            assert "not newer" in reason

    def test_http_rejected(self, mock_config):
        ok, reason = updater.validate_notice(
            _notice(download_url="http://opc.example.com/pkg.exe")
        )
        assert not ok
        assert "https" in reason

    def test_host_not_in_whitelist(self, mock_config):
        ok, reason = updater.validate_notice(
            _notice(download_url="https://evil.example.net/pkg.exe")
        )
        assert not ok
        assert "whitelist" in reason

    def test_whitelist_override(self):
        cfg = {
            "server_url": "wss://opc.example.com/opcgeo/agent/ws",
            "update_domain_whitelist": ["cdn.example.net"],
        }
        with patch.object(updater, "load_config", return_value=cfg):
            ok, reason = updater.validate_notice(
                _notice(download_url="https://cdn.example.net/pkg/sau-9.9.9.exe")
            )
            assert ok, reason
            # 原 server host 此时不再放行
            ok, _ = updater.validate_notice(
                _notice(download_url="https://opc.example.com/pkg.exe")
            )
            assert not ok

    def test_subdomain_allowed(self, mock_config):
        ok, reason = updater.validate_notice(
            _notice(download_url="https://dl.opc.example.com/pkg.exe")
        )
        assert ok, reason

    def test_empty_whitelist(self):
        with patch.object(updater, "load_config", return_value={"server_url": ""}):
            ok, reason = updater.validate_notice(_notice())
            assert not ok
            assert "whitelist" in reason
