"""Agent run tracing to LangSmith. On when LANGSMITH_API_KEY is set.

운영(실제 메일)에서는 메일 원문·값이 미국 LangSmith에 남지 않도록 입력·출력을 가린다(LANGSMITH_HIDE_CONTENT, 기본 켬).
가려도 실행 흐름(역할·도구 이름·상태 코드·다음 행동·검증 판정·지적 코드)과 토큰·시간·오류 종류는 남는다.
개발(합성 데이터)에서만 LANGSMITH_HIDE_CONTENT=false로 원문까지 본다."""
import re
from contextlib import contextmanager
from functools import lru_cache

from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import Client, tracing_context

# 가린 값 자리에 남기는 표시
MASK = '[가림]'
# 남겨 두는 글자 값: 영문·숫자·밑줄·하이픈·점·콜론으로만 된 짧은 값 (상태 코드·다음 행동·필드 이름·도구 이름·ID·이름표)
_CODE_LIKE = re.compile(r'[A-Za-z_][A-Za-z0-9_.:\-]{0,63}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def mask_content(value):
    """기록할 값에서 메일 원문·업체명·단가 같은 내용을 지운다. 구조(키·목록 길이)와 코드처럼 생긴 짧은 글자 값,
    참·거짓·null은 남긴다. 숫자는 금액·수량일 수 있어 지우고, 토큰 수 같은 실행 정보는 LangSmith가 따로 기록한다."""
    if isinstance(value, dict):
        return {key: mask_content(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_content(item) for item in value]
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str) and _CODE_LIKE.fullmatch(value) and not re.fullmatch(r'[0-9.:\-]+', value):
        return value
    return MASK


def _anonymize(data: dict) -> dict:
    """LangSmith 클라이언트가 보내기 전에 입력·출력·오류에 적용하는 가림. 오류({'error': 문자열})는 예외 종류만 남긴다
    (오류 문장·트레이스백에 메일 값·업체 ID가 들어갈 수 있다)."""
    if isinstance(data, dict) and set(data) == {'error'} and isinstance(data['error'], str):
        kind = re.match(r'\s*([A-Za-z_][A-Za-z0-9_.]*)', data['error'])
        return {'error': f"{kind.group(1) if kind else 'Error'}: {MASK}"}
    return mask_content(data)


@lru_cache
def _client(api_key: str, workspace_id: str | None = None, hide: bool = True) -> Client:
    """API 키·작업 공간·가림 여부별 LangSmith 클라이언트를 프로세스에서 하나만 만든다.
    가리면 입력·출력·오류를 _anonymize로 바꿔 보낸다(검증자 기록 @traceable도 같은 클라이언트로 보낸다)."""
    return Client(api_key=api_key, workspace_id=workspace_id, **({'anonymizer': _anonymize} if hide else {}))


@contextmanager
def agent_tracing(settings, **metadata):
    """에이전트 실행 구간의 LangSmith 기록을 정한다.

    LANGSMITH_API_KEY가 있으면 기록하고, 업무 ID 등 metadata와 환경 이름을 붙여 검색할 수 있게 한다.
    LANGSMITH_HIDE_CONTENT가 켜져 있으면(기본) 입력·출력 내용을 가린다. 키가 없으면 환경변수와 무관하게 기록을 끈다."""
    key = settings.langsmith_api_key.get_secret_value() if settings.langsmith_api_key else ''
    if not key:
        with tracing_context(enabled=False):
            yield
        return
    client = _client(key, settings.langsmith_workspace_id, settings.langsmith_hide_content)
    meta = {k: str(v) for k, v in metadata.items() if v is not None}
    meta |= {'environment': settings.app_environment, 'content_hidden': str(settings.langsmith_hide_content).lower()}
    with tracing_context(enabled=True, project_name=settings.langsmith_project, metadata=meta, client=client), \
            tracing_v2_enabled(settings.langsmith_project, client=client):
        yield
