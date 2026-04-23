"""Runtime configuration loaded from environment variables and optional .env files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://supromen.bookedscheduler.io"
DEFAULT_COOKIE_FILE = Path.home() / ".supromen" / "cookies.txt"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    email: str | None
    password: str | None
    cookie_file: Path

    @property
    def web_root(self) -> str:
        return f"{self.base_url.rstrip('/')}/Web"

    def require_credentials(self) -> tuple[str, str]:
        if not self.email or not self.password:
            raise ConfigError(
                "Missing credentials. Set SUPROMEN_EMAIL and SUPROMEN_PASSWORD "
                "(or pass --email / --password)."
            )
        return self.email, self.password


def _load_dotenv(path: Path) -> None:
    """Lightweight .env loader: does not override already-set env vars."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_settings(
    *,
    dotenv: Path | None = Path(".env"),
    email: str | None = None,
    password: str | None = None,
    base_url: str | None = None,
    cookie_file: Path | None = None,
) -> Settings:
    if dotenv is not None:
        _load_dotenv(dotenv)

    resolved_cookie = (
        cookie_file
        or (Path(os.environ["SUPROMEN_COOKIE_FILE"]).expanduser()
            if os.environ.get("SUPROMEN_COOKIE_FILE")
            else DEFAULT_COOKIE_FILE)
    )

    return Settings(
        base_url=base_url or os.environ.get("SUPROMEN_BASE_URL", DEFAULT_BASE_URL),
        email=email or os.environ.get("SUPROMEN_EMAIL"),
        password=password or os.environ.get("SUPROMEN_PASSWORD"),
        cookie_file=resolved_cookie,
    )
