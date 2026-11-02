"""Backend-owned boundary; agent internals belong to the agent team's PR.

An agent-owned app.agents.backend_adapter opts in with CONTRACT_VERSION=1,
EXTRACTION_ONLY=True and run_extraction(settings, scope). It must use the
backend evidence/stage tools, return a backend ExtractionOutcome, and never
persist or dispatch independently. No fallback prompt or graph is supplied here.
"""
from importlib import import_module
from app.core.errors import ServiceError


def load_extraction_adapter():
    try:
        module = import_module('app.agents.backend_adapter')
        if (type(getattr(module, 'CONTRACT_VERSION', None)) is not int
                or module.CONTRACT_VERSION != 1
                or getattr(module, 'EXTRACTION_ONLY', None) is not True
                or not callable(getattr(module, 'run_extraction', None))):
            raise ValueError('Incompatible adapter')
        return module.run_extraction
    except Exception:
        raise ServiceError('AGENT_INTEGRATION_REQUIRED',
            '정보 추출 에이전트 연결 계약을 먼저 준비해야 합니다.', 503,
            retryable=False, next_action='CONTACT_SERVER_ADMIN') from None


def extraction_adapter_available():
    try:
        load_extraction_adapter()
        return True
    except ServiceError:
        return False
