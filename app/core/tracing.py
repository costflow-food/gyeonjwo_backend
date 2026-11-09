"""Agent run tracing to LangSmith. Always on when LANGSMITH_API_KEY is set (MVP uses synthetic data only)."""
from contextlib import contextmanager
from functools import lru_cache

from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import Client, tracing_context


@lru_cache
def _client(api_key: str, workspace_id: str | None = None) -> Client:
    """API 키·작업 공간별 LangSmith 클라이언트를 프로세스에서 하나만 만든다."""
    return Client(api_key=api_key, workspace_id=workspace_id)


@contextmanager
def agent_tracing(settings, **metadata):
    """에이전트 실행 구간의 LangSmith 기록을 정한다.

    LANGSMITH_API_KEY가 있으면 항상 기록하고, 업무 ID 등 metadata를 붙여 검색할 수 있게 한다.
    키가 없으면 환경변수와 무관하게 기록을 끈다."""
    key = settings.langsmith_api_key.get_secret_value() if settings.langsmith_api_key else ''
    if not key:
        with tracing_context(enabled=False):
            yield
        return
    client = _client(key, settings.langsmith_workspace_id)
    meta = {k: str(v) for k, v in metadata.items() if v is not None}
    with tracing_context(enabled=True, project_name=settings.langsmith_project, metadata=meta, client=client), \
            tracing_v2_enabled(settings.langsmith_project, client=client):
        yield
