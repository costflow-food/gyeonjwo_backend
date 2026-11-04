"""BE-1 read-only preflight on the actual API host.

Run: python -m scripts.check_graph_rollout --db
Never claims/drains jobs, invokes a model, sends mail or changes configuration.
Only a successful live DB check can return exit code 0. This does not prove
that a running API process uses these settings or that a real case completed.
"""
import argparse
import json
from app.core.config import get_settings
from app.features.work_cases.application.start_readiness import configuration_readiness, agent_readiness
from app.features.work_cases.infrastructure.graph_repository import GraphRuntimeRepository
from app.features.work_cases.infrastructure.candidate_repository import CandidateRepository
from app.shared.infrastructure.db.client import make_server_client


def report(settings, *, db=False, client=None):
    checks = {
        'mail_sending_disabled': settings.rfq_mail_sending_enabled is False,
        'configuration_ready': configuration_readiness(settings).available,
        'agent_interface_ready': agent_readiness().available,
        'legacy_queue_drained': False,
        'case_graph_ready': False,
        'candidate_selection_ready': False,
    }
    database_code = 'NOT_CHECKED'
    if db:
        try:
            client = client if client is not None else make_server_client(settings)
        except Exception:
            client = None
        unavailable = client is None
        try:
            if client is None:
                raise ValueError('No server client')
            graph_repo = GraphRuntimeRepository(client)
            health = graph_repo.call('case_graph_health')
            checks['legacy_queue_drained'] = isinstance(health, dict) and health.get('legacy_queue_drained') is True
            checks['case_graph_ready'] = graph_repo.health_ready(health)
        except Exception:
            unavailable = True
        try:
            if client is None:
                raise ValueError('No server client')
            candidate = CandidateRepository(client).ready()
            checks['candidate_selection_ready'] = candidate.available
        except Exception:
            unavailable = True
        # One failed probe must not hide the other independent DB contract.
        # Never print SDK messages, settings or arbitrary RPC JSON.
        database_code = ('PROCESSING_UNAVAILABLE' if unavailable else 'READY' if all(checks[key] for key in (
            'legacy_queue_drained', 'case_graph_ready', 'candidate_selection_ready')) else 'NOT_READY')
    return {
        'scope': 'BE1_PREREQUISITES_ONLY', 'ready': db and all(checks.values()),
        'checks': checks, 'database_checked': db, 'database_code': database_code,
        'execution_mode': 'GRAPH',
        'live_api_process_verified': False, 'real_case_verified': False,
        'writes_performed': False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='store_true', help='Read-only graph and candidate health RPCs.')
    args = parser.parse_args()
    result = report(get_settings(), db=args.db)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
