from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    supabase_url: HttpUrl | None = None
    supabase_secret_key: SecretStr | None = None
    supabase_publishable_key: SecretStr | None = None
    llm_base_url: HttpUrl | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str = ''
    llm_classification_model: str = ''
    llm_timeout_seconds: int = Field(default=60, ge=5, le=180)
    analysis_timeout_seconds: int = Field(default=600, ge=30, le=1800)
    calculation_rounds: int = Field(default=3, ge=2, le=5)
    max_document_characters: int = Field(default=120000, ge=1000, le=300000)
    cors_origins: list[str] = []
    gmail_email: str = ''
    gmail_client_id: str = ''
    gmail_client_secret: SecretStr | None = None
    gmail_refresh_token: SecretStr | None = None
    gmail_topic: str = ''
    gmail_subscription: str = ''
    pubsub_audience: str = ''
    pubsub_service_account: str = ''
    worker_enabled: bool = False
    worker_poll_seconds: int = Field(default=10, ge=1, le=60)
    worker_sync_seconds: int = Field(default=300, ge=60, le=3600)

    @field_validator('supabase_url', 'llm_base_url', 'supabase_secret_key', 'supabase_publishable_key',
                     'llm_api_key', 'gmail_client_secret', 'gmail_refresh_token', mode='before')
    @classmethod
    def empty_optional_values(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    def require_llm(self):
        if not self.llm_base_url or not self.llm_api_key or not self.llm_api_key.get_secret_value() or not self.llm_model:
            raise RuntimeError('LLM endpoint, key and model must be configured')

    def require_gmail(self):
        names = ('gmail_email', 'gmail_client_id', 'gmail_client_secret', 'gmail_refresh_token',
                 'gmail_topic', 'gmail_subscription', 'pubsub_audience', 'pubsub_service_account')
        if any(not getattr(self, name) for name in names):
            raise RuntimeError('Gmail OAuth and Pub/Sub settings must be configured')

    def require_server(self) -> None:
        if not self.supabase_url or not self.supabase_secret_key or not self.supabase_secret_key.get_secret_value():
            raise RuntimeError('SUPABASE_URL and SUPABASE_SECRET_KEY must be configured locally.')


@lru_cache
def get_settings() -> Settings:
    return Settings()
