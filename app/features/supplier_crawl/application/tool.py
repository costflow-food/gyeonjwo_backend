"""Preserved opt-in KAN-109 registration binding, not a role tool or installer.

The collection @tool is agents.tools.crawl_supplier_candidates, intentionally
absent from every ROLE_TOOLS. Registration remains a separate explicit service.
No Runner/graph/startup binding or automatic activation is performed here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from app.agents.schemas import RunContext
from app.features.supplier_crawl.domain.registration import RegistrationError

_service = ContextVar('supplier_crawl_service', default=None)


@contextmanager
def bind_crawl_service(service):
    token = _service.set(service)
    try:
        yield
    finally:
        _service.reset(token)


def run_crawl_registration(context: RunContext | None) -> dict:
    """Invoke only an explicitly bound KAN service; never install a role tool."""
    service = _service.get()
    if service is None or context is None:
        return {'status': 'NOT_CONFIGURED'}
    try:
        return service.run(context)
    except RegistrationError as error:
        return {'status': 'BLOCKED', 'code': error.code}
