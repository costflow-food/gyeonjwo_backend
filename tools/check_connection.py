"""Run locally after configuring .env; never prints credentials or DB rows."""

from costflow.config import Settings
from costflow.errors import AppError
from costflow.gateway import SupabaseGateway

TABLES = (
    "suppliers", "items", "contracts", "purchase_plans", "price_references",
    "cost_terms", "work_items", "request_fields", "review_results",
)


def verify(gateway, settings):
    for table in TABLES:
        gateway.rows(table, limit=1)
    from urllib.parse import quote

    bucket = gateway.request("GET", "/storage/v1/bucket/" + quote(settings.bucket, safe=""))
    if not isinstance(bucket, dict) or bucket.get("public") is not False:
        raise AppError(503, "bucket_not_private", "파일 버킷의 비공개 설정을 확인해 주세요.")
    for user_id in settings.users | settings.reviewers:
        user = gateway.request("GET", "/auth/v1/admin/users/" + user_id)
        if not isinstance(user, dict) or user.get("id") != user_id:
            raise AppError(503, "unknown_user", "허용 목록의 Auth 사용자 UUID를 확인해 주세요.")


def main():
    settings = Settings.from_env()
    if not settings.ready:
        print("Supabase URL 또는 서버 전용 키가 설정되지 않았습니다.")
        return 2
    if not settings.users and not settings.reviewers:
        print("CostFlow 팀 사용자·검토자 UUID 허용 목록을 먼저 설정해 주세요.")
        return 2
    try:
        verify(SupabaseGateway(settings), settings)
    except AppError as exc:
        print(f"연결 실패: {exc.code} — {exc.message}")
        return 1
    print("Supabase 9테이블 조회·private 버킷·Auth 사용자 존재 확인 성공. 데이터·키는 출력하지 않았습니다.")
    print("쓰기 RPC·실제 사용자 로그인·Storage 업로드 검증은 별도로 필요합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
