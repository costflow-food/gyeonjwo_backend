from functools import lru_cache

from supabase import Client, ClientOptions, create_client

from app.core.config import get_settings


@lru_cache
def get_server_client() -> Client:
    return make_server_client(get_settings())


def make_server_client(settings) -> Client:
    settings.require_server()
    return create_client(
        str(settings.supabase_url).rstrip('/'),
        settings.supabase_secret_key.get_secret_value(),
        options=ClientOptions(persist_session=False, auto_refresh_token=False,
                              postgrest_client_timeout=15, storage_client_timeout=30),
    )
