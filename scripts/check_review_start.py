"""Read-only prerequisites check on the API host: python -m scripts.check_review_start --db.

No credentials, user records, model calls or job claims are printed or performed.
This separate CLI process cannot attest that the running API worker is alive;
list/detail disabled_actions reflect the API process's live worker status.
"""
import argparse
import json
from app.core.config import get_settings
from app.features.work_cases.application.start_readiness import (
    configuration_readiness, agent_readiness, database_readiness)
from app.features.work_cases.infrastructure.graph_repository import runtime_repository
from app.shared.infrastructure.db.client import make_server_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='store_true', help='Read-only DB health RPC; no migration or writes.')
    args = parser.parse_args()
    settings = get_settings()
    stages = {'configuration': configuration_readiness(settings), 'agent': agent_readiness(settings.agent_graph_enabled)}
    if args.db:
        try:
            stages['database'] = database_readiness(runtime_repository(make_server_client(settings), settings))
        except Exception:
            from app.features.work_cases.application.start_readiness import StartReadiness
            stages['database'] = StartReadiness('PROCESSING_UNAVAILABLE', 'DB 연결 설정을 확인해 주세요.')
    report = {'scope': 'prerequisites_only',
        'execution_mode': 'GRAPH_START' if settings.agent_graph_enabled else 'EXTRACTION_ONLY', 'checks': {
        key: {'ready': value.available, 'code': value.code, 'fields': list(value.fields)}
        for key, value in stages.items()},
        'worker_setting_enabled': settings.agent_extraction_worker_enabled,
        'live_worker_verified': False, 'database_checked': args.db}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if all(value.available for value in stages.values()) and settings.agent_extraction_worker_enabled else 1


if __name__ == '__main__':
    raise SystemExit(main())
