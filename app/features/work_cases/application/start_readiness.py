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
    if not settings.agent_extraction_enabled:
        return StartReadiness('EXTRACTION_DISABLED', '서버의 검토 실행 설정이 꺼져 있습니다.',
                              ('AGENT_EXTRACTION_ENABLED',))
    if not settings.agent_stop_after_extraction:
        return StartReadiness('EXTRACTION_MODE_REQUIRED', '서버를 ① 정보 추출 전용 실행 모드로 설정해야 합니다.',
                              ('AGENT_STOP_AFTER_EXTRACTION',))
    missing = tuple(name.upper() for name in ('llm_base_url', 'llm_api_key', 'llm_model')
                    if not getattr(settings, name))
    if missing:
        return StartReadiness('EXTRACTION_CONFIGURATION_REQUIRED', '서버의 LLM 연결 설정이 필요합니다.', missing)
    return StartReadiness()


def agent_readiness():
    from app.features.work_cases.application.agent_boundary import extraction_adapter_available
    if not extraction_adapter_available():
        return StartReadiness('AGENT_INTEGRATION_REQUIRED', '정보 추출 에이전트 연결 코드를 확인해야 합니다.')
    return StartReadiness()


def database_readiness(repo):
    try:
        if repo.available():
            return StartReadiness()
        return StartReadiness('EXTRACTION_SCHEMA_REQUIRED', '검토 실행·견적 저장 DB 계약을 먼저 적용해야 합니다.')
    except ServiceError as error:
        if error.code == 'EXTRACTION_SCHEMA_REQUIRED':
            return StartReadiness('EXTRACTION_SCHEMA_REQUIRED', '검토 실행·견적 저장 DB 계약을 먼저 적용해야 합니다.')
        return StartReadiness('PROCESSING_UNAVAILABLE', '검토 실행 저장소에 연결할 수 없습니다.')
    except Exception:
        return StartReadiness('PROCESSING_UNAVAILABLE', '검토 실행 저장소를 확인할 수 없습니다.')


def worker_readiness(settings, worker):
    if not settings.agent_extraction_worker_enabled:
        return StartReadiness('EXTRACTION_WORKER_DISABLED', '서버의 정보 추출 워커 설정이 꺼져 있습니다.',
                              ('AGENT_EXTRACTION_WORKER_ENABLED',))
    if worker is None or not worker.running:
        return StartReadiness('EXTRACTION_WORKER_UNAVAILABLE', '정보 추출 워커가 실행 중이 아닙니다. 서버 상태를 확인해 주세요.')
    return StartReadiness()
