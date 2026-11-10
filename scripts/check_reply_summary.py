"""Read-only card 8 verification; prints no IDs, mail bodies or credentials.

python -m scripts.check_reply_summary --owner UUID --case UUID [--case UUID]
Only the trusted server operator may use this diagnostic; it is not an HTTP API.
"""
import argparse
import json
from uuid import UUID

from app.core.config import get_settings
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.list_repository import WorkCaseListRepository
from app.shared.infrastructure.db.client import make_server_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True, type=UUID)
    parser.add_argument('--case', required=True, action='append', type=UUID, dest='cases')
    args = parser.parse_args()
    if len(args.cases) > 100:
        parser.error('Provide 1 to 100 case IDs')
    try:
        repo = WorkCaseListRepository(make_server_client(get_settings()))
        result = repo.reply_summaries(str(args.owner), [str(value) for value in args.cases])
        print(json.dumps({'status': 'ready', 'verified_case_count': len(result)}))
        return 0
    except ServiceError as error:
        print(json.dumps({'status': 'not_ready', 'code': error.code}))
        return 1
    except Exception:
        print(json.dumps({'status': 'not_ready', 'code': 'PROCESSING_UNAVAILABLE'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
