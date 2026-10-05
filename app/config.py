"""Application settings loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    google_client_id: str = ""
    google_client_secret: str = ""
    secret_key: str = ""
    base_url: str = ""
    allowed_emails: list[str] = field(default_factory=list)
    downloads_dir: str = ""
    version: str = "2.0.0"
    git_sha: str = field(default_factory=lambda: os.getenv("BOOKQ_GIT_SHA", "unknown"))
    build_date: str = field(default_factory=lambda: os.getenv("BOOKQ_BUILD_DATE", "unknown"))

    @property
    def downloads_path(self) -> Path:
        path = Path(self.downloads_dir) if self.downloads_dir else (Path(__file__).parent.parent / "downloads")
        return path.resolve()

    @property
    def allowed_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails if e.strip()}


def load_settings() -> Settings:
    """Load settings from environment variables. .env is read by docker compose env_file."""
    allowed_raw = os.getenv("ALLOWED_EMAILS", "")
    secret_key = os.getenv("SECRET_KEY", "")
    # Placeholder from .env.example is public — accepting it would let anyone
    # forge the signed session cookie that get_current_user trusts (auth bypass).
    if (not secret_key.strip() or secret_key.strip() == "change-me-to-a-random-64-char-string") and os.getenv("BOOKQ_TEST_MODE", "").lower() != "true":
        raise RuntimeError("SECRET_KEY is not set; set a random value (e.g. `openssl rand -hex 32`)")
    return Settings(
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        secret_key=secret_key,
        base_url=os.getenv("BASE_URL", ""),
        allowed_emails=[e.strip() for e in allowed_raw.split(",") if e.strip()],
        downloads_dir=os.getenv("DOWNLOADS_DIR", ""),
    )
