from functools import lru_cache
from httpx import Client as HttpClient, Limits

from supabase import Client, ClientOptions, create_client

from app.core.config import get_settings
from app.core.upstream_logs import observe_data_api


@lru_cache
def get_server_client() -> Client:
    return make_server_client(get_settings())


def make_server_client(settings, *, http1_only=False) -> Client:
    settings.require_server()
    # The list polls at 15s. Keep idle connections long enough for reuse.
    http_client = (HttpClient(http2=False, timeout=15, limits=Limits(
        max_connections=100, max_keepalive_connections=20, keepalive_expiry=30))
        if http1_only else None)
    client = create_client(
        str(settings.supabase_url).rstrip('/'),
        settings.supabase_secret_key.get_secret_value(),
        options=ClientOptions(persist_session=False, auto_refresh_token=False,
                              postgrest_client_timeout=15, storage_client_timeout=30,
                              httpx_client=http_client),
    )
    observe_data_api(client.postgrest.session)
    return client
