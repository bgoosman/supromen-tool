from __future__ import annotations

import pytest

from supromen.config import ConfigError, load_settings


def test_load_settings_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPROMEN_EMAIL", "foo@example.com")
    monkeypatch.setenv("SUPROMEN_PASSWORD", "pw")
    monkeypatch.setenv("SUPROMEN_BASE_URL", "https://example.test")
    monkeypatch.setenv("SUPROMEN_COOKIE_FILE", str(tmp_path / "c.txt"))

    s = load_settings(dotenv=None)

    assert s.email == "foo@example.com"
    assert s.password == "pw"
    assert s.web_root == "https://example.test/Web"
    assert s.cookie_file == tmp_path / "c.txt"


def test_require_credentials_raises_when_missing(monkeypatch):
    monkeypatch.delenv("SUPROMEN_EMAIL", raising=False)
    monkeypatch.delenv("SUPROMEN_PASSWORD", raising=False)
    s = load_settings(dotenv=None)
    with pytest.raises(ConfigError):
        s.require_credentials()
