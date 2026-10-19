from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlsplit
from uuid import UUID


def read_dotenv(path: Path) -> dict[str, str]:
    """Read literal assignments only; no shell execution or variable expansion."""
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip().replace("_", "").isalnum():
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    return values


def user_ids(value: str) -> frozenset[str]:
    return frozenset(str(UUID(v.strip())) for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    supabase_url: str = ""
    service_key: str = field(default="", repr=False)
    bucket: str = "costflow-files"
    users: frozenset[str] = frozenset()
    reviewers: frozenset[str] = frozenset()
    cors_origins: tuple[str, ...] = ()
    max_upload_bytes: int = 50 * 1024 * 1024
    timeout_seconds: float = 20

    @property
    def ready(self) -> bool:
        return bool(self.supabase_url and self.service_key)

    @classmethod
    def from_env(cls) -> "Settings":
        env = read_dotenv(Path(__file__).resolve().parents[2] / ".env") | dict(os.environ)
        url = env.get("SUPABASE_URL", "").rstrip("/")
        if url:
            p = urlsplit(url)
            local = p.hostname in {"127.0.0.1", "localhost"}
            if p.username or p.password or p.query or p.fragment or p.path not in {"", "/"}:
                raise ValueError("SUPABASE_URL must be an origin without credentials or a path")
            if p.scheme != "https" and not (local and p.scheme == "http"):
                raise ValueError("SUPABASE_URL requires HTTPS (except local development)")
        max_bytes = int(env.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
        if not 1 <= max_bytes <= 50 * 1024 * 1024:
            raise ValueError("MAX_UPLOAD_BYTES must be between 1 and 52428800")
        timeout = float(env.get("SUPABASE_TIMEOUT_SECONDS", "20"))
        if not 0 < timeout <= 120:
            raise ValueError("SUPABASE_TIMEOUT_SECONDS must be between 0 and 120")
        return cls(
            supabase_url=url,
            service_key=env.get("SUPABASE_SERVICE_KEY", ""),
            bucket=env.get("SUPABASE_BUCKET", "costflow-files"),
            users=user_ids(env.get("COSTFLOW_USER_IDS", "")),
            reviewers=user_ids(env.get("COSTFLOW_REVIEWER_IDS", "")),
            cors_origins=tuple(
                v.strip() for v in env.get("CORS_ORIGINS", "").split(",") if v.strip()
            ),
            max_upload_bytes=max_bytes,
            timeout_seconds=timeout,
        )
