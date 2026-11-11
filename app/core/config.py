from functools import lru_cache
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import HttpUrl, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
# OpenAI reasoning effort 값 (2026-11-02 문서 기준). 모델마다 받는 값이 다르다 (예: gpt-5.4-mini는 minimal·max 없음)
ReasoningEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    supabase_url: HttpUrl | None = None
    supabase_secret_key: SecretStr | None = None
    supabase_publishable_key: SecretStr | None = None
    llm_base_url: HttpUrl | None = None
    llm_api_key: SecretStr | None = None
    # 모델·추론 수준·시간 제한은 env가 아닌 코드 상수다. 모델과 추론 수준은 한 쌍으로 바꾼다
    llm_model: ClassVar[str] = 'gpt-6-luna'
    llm_classification_model: ClassVar[str] = 'gpt-5-nano'
    llm_classification_reasoning_effort: ClassVar[ReasoningEffort] = 'minimal'
    # 에이전트 역할별 모델. 없는 역할은 llm_model을 쓴다
    llm_role_models: ClassVar[dict[str, str]] = {}
    # 에이전트 역할별 추론 수준. 없는 역할은 llm_reasoning_effort(None이면 보내지 않음)
    llm_reasoning_effort: ClassVar[ReasoningEffort | None] = None
    llm_role_reasoning_efforts: ClassVar[dict[str, ReasoningEffort]] = {
        'INFO_EXTRACTOR': 'low', 'CANDIDATE': 'low', 'INFO_PRIORITY': 'medium',
        'RESPONSE_PLAN': 'medium', 'COUNTER_OFFER': 'low'}
    # LLM 검증자(에이전트 결과를 원문·도구 결과와 대조). 여기에 추론 수준이 있는 역할만 검증한다.
    # ②③⑤는 초안 도구를 실행하기 전에, ①④는 최종 답을 검증한다
    llm_verifier_model: ClassVar[str] = 'gpt-6-luna'
    llm_verifier_reasoning_efforts: ClassVar[dict[str, ReasoningEffort]] = {
        'INFO_EXTRACTOR': 'high', 'CANDIDATE': 'high', 'INFO_PRIORITY': 'high', 'RESPONSE_PLAN': 'high',
        'COUNTER_OFFER': 'high'}
    llm_timeout_seconds: ClassVar[int] = 60
    analysis_timeout_seconds: ClassVar[int] = 600
    calculation_rounds: ClassVar[int] = 3
    max_document_characters: ClassVar[int] = 120000
    # 최초 메일함 가져오기와 만료된 history 복구에만 쓴다. 새 메일은 개수 제한이 없다
    gmail_initial_sync_limit: ClassVar[int] = 10
    agent_graph_review_timeout_seconds: ClassVar[int] = 300
    agent_graph_timeout_seconds: ClassVar[int] = 180
    agent_graph_request_timeout_seconds: ClassVar[int] = 45
    cors_origins: list[str] = ['http://localhost:3000']
    gmail_client_id: str = ''
    gmail_client_secret: SecretStr | None = None
    gmail_topic: str = ''
    gmail_subscription: str = ''
    pubsub_audience: str = ''
    pubsub_service_account: str = ''
    google_token_encryption_key: SecretStr | None = None
    # 빌드 시 주입한다
    app_build_revision: str = 'development'
    # 에이전트 실행은 키가 있으면 LangSmith에 기록한다. 키가 없을 때만 기록하지 않는다
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = 'gyeonjwo-dev'
    # 기록의 입력·출력 내용(메일 원문·업체명·단가)을 가린다. 기본 켬(운영). 합성 데이터로 개발할 때만 false로 둔다
    langsmith_hide_content: bool = True
    # 실행 환경 이름. LangSmith 기록 metadata(environment)로 운영·개발 기록을 구분한다(운영은 production)
    app_environment: str = 'development'
    # 조직 단위 Service Key는 기록할 작업 공간 ID가 있어야 한다(LangSmith Settings 화면에서 확인)
    langsmith_workspace_id: str | None = None

    @field_validator('supabase_url', 'llm_base_url', 'supabase_secret_key', 'supabase_publishable_key',
                     'llm_api_key', 'gmail_client_secret', 'google_token_encryption_key', 'langsmith_api_key',
                     'langsmith_workspace_id', mode='before')
    @classmethod
    def empty_optional_values(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    def require_llm(self):
        if not self.llm_base_url or not self.llm_api_key or not self.llm_api_key.get_secret_value():
            raise RuntimeError('LLM endpoint and key must be configured')

    def llm_model_for(self, role: str) -> str:
        """에이전트 역할(INFO_EXTRACTOR 등)에 쓸 모델. 역할별 상수가 없으면 llm_model."""
        return self.llm_role_models.get(str(role), self.llm_model)

    def llm_reasoning_effort_for(self, role: str | None) -> str | None:
        """에이전트 역할에 쓸 추론 수준. 역할별 상수가 없으면 llm_reasoning_effort, 둘 다 없으면 None(보내지 않음)."""
        return self.llm_role_reasoning_efforts.get(str(role), self.llm_reasoning_effort) if role else self.llm_reasoning_effort

    def require_classification(self):
        if not self.llm_base_url or not self.llm_api_key:
            raise RuntimeError('Classification endpoint and key must be configured')

    def require_server(self) -> None:
        if not self.supabase_url or not self.supabase_secret_key or not self.supabase_secret_key.get_secret_value():
            raise RuntimeError('SUPABASE_URL and SUPABASE_SECRET_KEY must be configured locally.')
        if self.supabase_url.host.casefold() == 'your_project.supabase.co':
            raise RuntimeError('Supabase project URL is still a template')

    def require_google_connect(self):
        self.require_server()
        if not self.gmail_client_id or not self.gmail_client_secret or not self.google_token_encryption_key:
            raise RuntimeError('Google OAuth client and token encryption key must be configured')

    @property
    def gmail_push_configured(self):
        return all((self.gmail_topic,self.gmail_subscription,self.pubsub_audience,self.pubsub_service_account))


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        fields = sorted({str(item['loc'][0]).upper() for item in error.errors(include_input=False) if item['loc']})
        raise SystemExit(', '.join(fields) or 'ENV_CONFIGURATION') from None
    except Exception:
        # Invalid dotenv/JSON settings may include raw input in their exception text.
        raise SystemExit('ENV_CONFIGURATION') from None
