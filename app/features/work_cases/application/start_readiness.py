"""Read-only review-start prerequisites shared by HTTP and local diagnostics.

Only fixed codes, field names and safe messages are exposed. No provider calls,
model invocations, job claims or credentials are included in the report.
"""
from dataclasses import dataclass
from app.core.errors import ServiceError


@dataclass(frozen=True)
class StartReadiness:
    code: str | None = None
    message: str = ''
    fields: tuple[str, ...] = ()

    @property
    def available(self):
        return self.code is None

    def disabled_action(self):
        return {'action': 'START', 'reason_code': self.code, 'message': self.message}

    def require(self):
        if not self.available:
            raise ServiceError(self.code, self.message, 503, fields=list(self.fields),
                retryable=self.code in {'EXTRACTION_WORKER_UNAVAILABLE', 'PROCESSING_UNAVAILABLE'},
                next_action='CONTACT_SERVER_ADMIN')


def configuration_readiness(settings):
    missing = tuple(name.upper() for name in ('llm_base_url', 'llm_api_key', 'llm_model')
                    if not getattr(settings, name))
    if missing:
        return StartReadiness('EXTRACTION_CONFIGURATION_REQUIRED', '서버의 LLM 연결 설정이 필요합니다.', missing)
    return StartReadiness()


def agent_readiness():
    try:
        from app.features.work_cases.application.graph_boundary import graph_contract
        graph_contract(review_enabled=True)
        return StartReadiness()
    except Exception:
        return StartReadiness('AGENT_INTEGRATION_REQUIRED', '에이전트 그래프 공개 인터페이스를 확인해야 합니다.')


def database_readiness(repo):
    try:
        if repo.available():
            return StartReadiness()
        return StartReadiness('EXTRACTION_SCHEMA_REQUIRED', 'Supabase의 검토 실행·견적 저장 함수 구성을 확인해야 합니다.')
    except ServiceError as error:
        if error.code == 'EXTRACTION_SCHEMA_REQUIRED':
            return StartReadiness('EXTRACTION_SCHEMA_REQUIRED', 'Supabase의 검토 실행·견적 저장 함수 구성을 확인해야 합니다.')
        return StartReadiness('PROCESSING_UNAVAILABLE', '검토 실행 저장소에 연결할 수 없습니다.')
    except Exception:
        return StartReadiness('PROCESSING_UNAVAILABLE', '검토 실행 저장소를 확인할 수 없습니다.')


def worker_readiness(settings, worker):
    if worker is None or not worker.running:
        return StartReadiness('EXTRACTION_WORKER_UNAVAILABLE', '정보 추출 워커가 실행 중이 아닙니다. 서버 상태를 확인해 주세요.')
    return StartReadiness()
