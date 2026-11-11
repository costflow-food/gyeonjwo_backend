"""Supabase 연결 점검: python -m scripts.check_connection (읽기만 하고 비밀값은 출력하지 않는다)."""
import sys

from app.shared.infrastructure.db.client import get_server_client

# 서버 코드가 직접 읽는 테이블. 행은 가져오지 않고(limit 0) 테이블·권한만 확인한다
TABLES = ('work_cases', 'mail_messages', 'mail_drafts', 'quote_versions', 'agent_runs', 'workflow_logs',
          'suppliers', 'supplier_items', 'purchase_history', 'mail_sync_states', 'mail_send_settings')


def check(client):
    """테이블마다 빈 조회를 보내 접근 실패한 테이블 이름을 돌려준다."""
    failed = []
    for table in TABLES:
        try:
            client.table(table).select('*').limit(0).execute()
        except Exception:
            failed.append(table)
    return failed


if __name__ == '__main__':
    try:
        failed = check(get_server_client())
    except Exception:
        print('연결 점검 실패: .env의 Supabase URL·서버 키를 확인해 주세요.')
        sys.exit(1)
    if failed:
        print('읽지 못한 테이블:', ', '.join(failed))
        sys.exit(1)
    print(f'읽기 전용 연결 점검 통과: 테이블 {len(TABLES)}개')
