"""Interactive local setup. Credentials never travel through chat or command arguments."""

import base64
import binascii
import getpass
import json
import os
from pathlib import Path
import tempfile
import warnings

from costflow.config import Settings, read_dotenv, user_ids

ROOT = Path(__file__).resolve().parents[1]


def validate_key(value: str) -> str:
    value = value.strip()
    if any(ch.isspace() for ch in value):
        raise ValueError("서버 키에 공백이나 줄바꿈이 포함되어 있습니다.")
    if value.startswith("sb_secret_") and len(value) > 20:
        return value
    try:
        payload = value.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError, TypeError, binascii.Error):
        raise ValueError("Supabase secret 키 또는 service_role 키가 필요합니다.") from None
    if not isinstance(claims, dict) or claims.get("role") != "service_role":
        raise ValueError("anon/publishable 키는 서버 전용 키로 사용할 수 없습니다.")
    # This only checks key type. The connection check verifies the real credential.
    return value


def save_env(path: Path, updates: dict[str, str]) -> None:
    current = read_dotenv(path)
    current.update(updates)
    for key, value in current.items():
        if not key.replace("_", "").isalnum() or any(c in value for c in "\r\n\x00"):
            raise ValueError("환경변수에 허용하지 않는 문자가 있습니다.")
        if value != value.strip() or value[:1] in {"'", '"'}:
            raise ValueError("환경변수 값의 앞뒤 공백·따옴표를 제거해 주세요.")
    # Atomic replacement; do not leave plaintext backup copies behind.
    fd, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write("# Local server configuration. Never commit this file.\n")
            output.writelines(f"{key}={value}\n" for key, value in current.items())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> int:
    path = ROOT / ".env"
    current = read_dotenv(path if path.exists() else ROOT / ".env.example")
    print("CostFlow 로컬 설정: 키는 이 컴퓨터의 .env에만 저장하며 화면에 표시하지 않습니다.")
    print("Supabase Settings > API Keys에서 서버 전용 secret/service_role 키를 확인하세요.")
    try:
        # If hidden console input is unavailable, abort instead of echoing the key.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            entered = getpass.getpass("서버 키 입력 (기존 설정 유지: Enter): ")
        key = validate_key(entered or current.get("SUPABASE_SERVICE_KEY", ""))
        users = input("일반 팀 사용자 UUID들 (쉼표 구분, 기존 값 유지: Enter): ").strip()
        reviewers = input("검토자 UUID들 (쉼표 구분, 기존 값 유지: Enter): ").strip()
        users = users or current.get("COSTFLOW_USER_IDS", "")
        reviewers = reviewers or current.get("COSTFLOW_REVIEWER_IDS", "")
        parsed_users, parsed_reviewers = user_ids(users), user_ids(reviewers)
        if not parsed_users and not parsed_reviewers:
            raise ValueError("Supabase Authentication > Users의 UUID를 한 개 이상 등록해 주세요.")
        current.update(
            SUPABASE_SERVICE_KEY=key,
            COSTFLOW_USER_IDS=",".join(sorted(parsed_users)),
            COSTFLOW_REVIEWER_IDS=",".join(sorted(parsed_reviewers)),
        )
        save_env(path, current)
        settings = Settings.from_env()
        if not settings.ready:
            raise ValueError("Supabase URL 설정이 필요합니다.")
    except (ValueError, EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        print("설정을 완료하지 못했습니다. 키는 출력하지 않았습니다. 입력 형식과 사용자 UUID를 확인하세요.")
        return 2
    print("로컬 설정 저장 완료. 다음으로 tools/check_connection.py를 실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
