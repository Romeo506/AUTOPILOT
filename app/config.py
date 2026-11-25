"""Runtime settings. Everything is read from environment variables (or .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent


def _bool(v: str | None, default=False) -> bool:
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(v: str | None, default: int) -> int:
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    password: str = ""
    # Optional integrations. All are OFF unless the user supplies credentials.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_ssl: bool = False  # True = implicit TLS (port 465); False = STARTTLS
    stripe_webhook_secret: str = ""
    business_name: str = "My Video Clipping Studio"
    payment_instructions: str = ""
    # Limits / safety
    max_upload_mb: int = 4096
    max_download_mb: int = 2048
    scan_interval_minutes: int = 0  # 0 = background scanning disabled
    allow_private_urls: bool = False  # only for tests / trusted LAN feeds
    import_dirs: list[Path] = field(default_factory=list)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        self.uploads = self.data_dir / "uploads"
        self.exports = self.data_dir / "exports"
        self.imports = self.data_dir / "imports"
        self.work = self.data_dir / "work"
        for d in (self.data_dir, self.uploads, self.exports, self.imports, self.work):
            d.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "autopilot.sqlite3"
        self.import_dirs = [Path(p).expanduser().resolve() for p in self.import_dirs]  # follow symlinks (Termux ~/storage/* are symlinks)
        if not self.import_dirs:
            home = Path.home()
            cands = [self.imports, home / "storage" / "movies", home / "storage" / "downloads",
                     home / "storage" / "dcim", home / "storage" / "shared"]
            self.import_dirs = [p.resolve() for p in cands if p.exists()]

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(BASE / ".env")
        g = os.getenv
        data = Path(g("DATA_DIR", "") or BASE / "data")
        if not data.is_absolute():
            data = BASE / data
        extra = [Path(p.strip()).expanduser() for p in (g("IMPORT_DIRS") or "").split(os.pathsep) if p.strip()]
        return cls(
            data_dir=data,
            password=(g("APP_PASSWORD") or "").strip(),
            smtp_host=(g("SMTP_HOST") or "").strip(),
            smtp_port=_int(g("SMTP_PORT"), 587),
            smtp_user=(g("SMTP_USER") or "").strip(),
            smtp_password=g("SMTP_PASSWORD") or "",
            smtp_from=(g("SMTP_FROM") or "").strip(),
            smtp_ssl=_bool(g("SMTP_SSL")),
            stripe_webhook_secret=(g("STRIPE_WEBHOOK_SECRET") or "").strip(),
            business_name=(g("BUSINESS_NAME") or "My Video Clipping Studio").strip(),
            payment_instructions=(g("PAYMENT_INSTRUCTIONS") or "").strip(),
            max_upload_mb=_int(g("MAX_UPLOAD_MB"), 4096),
            max_download_mb=_int(g("MAX_DOWNLOAD_MB"), 2048),
            scan_interval_minutes=_int(g("SCAN_INTERVAL_MINUTES"), 0),
            allow_private_urls=_bool(g("ALLOW_PRIVATE_URLS")),
            import_dirs=[p.resolve() for p in extra if p.exists()],
        )
