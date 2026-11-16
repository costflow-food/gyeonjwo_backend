"""Record Supabase failures without request headers, URLs or response bodies."""
from functools import wraps
import logging
from time import monotonic

import httpx

from app.core.request_context import current_request_id

logger = logging.getLogger('gyeonjwo')


def log_upstream_failure(stage, status, started):
    logger.warning(
        'upstream failure request_id=%s stage=%s upstream_status=%s duration_ms=%.1f',
        current_request_id(), stage, status if status is not None else 'none',
        (monotonic() - started) * 1000,
    )


def observe_data_api(session):
    """Observe the existing HTTP client without changing its transport or timeouts."""
    send = session.send

    @wraps(send)
    def observed_send(request, *args, **kwargs):
        if not request.url.path.startswith('/rest/v1/'):
            return send(request, *args, **kwargs)
        started = monotonic()
        try:
            response = send(request, *args, **kwargs)
        except httpx.RequestError:
            log_upstream_failure('data_api', None, started)
            raise
        if response.status_code >= 500:
            log_upstream_failure('data_api', response.status_code, started)
        return response

    session.send = observed_send
