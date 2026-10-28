"""Run: python -m scripts.check_connection (read-only, no secrets printed)."""
import sys

from app.shared.infrastructure.db.client import get_server_client
from app.shared.infrastructure.db.contracts import REFERENCE_COLUMNS, WORK_COLUMNS


def check(client):
    for table, columns in REFERENCE_COLUMNS.items():
        client.table(table).select(columns).limit(0).execute()
    client.table('work_items').select(WORK_COLUMNS).limit(0).execute()
    for name in ('gyeonjwo-files', 'gyeonjwo-reference-files'):
        bucket = client.storage.get_bucket(name)
        if bucket.public:
            raise RuntimeError('Expected private bucket')


if __name__ == '__main__':
    try:
        check(get_server_client())
    except Exception:
        print('Connection check failed. Check local .env, deployed v2.2 SQL, table grants and private buckets.')
        sys.exit(1)
    print('Read-only connection check passed: 7 reference tables, queue columns and 2 private buckets.')
