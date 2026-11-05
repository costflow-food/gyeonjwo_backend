from uuid import UUID
from fastapi import APIRouter, Depends, Request
from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.quotes import QuoteService
from app.features.work_cases.domain.quotes import QuotesResponse
from app.features.work_cases.infrastructure.quote_repository import QuoteRepository
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1/work-cases', tags=['Case quotes'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def get_quote_service(request: Request):
    """요청마다 같은 견적 조회 서비스를 쓴다."""
    with request.app.state.service_lock:
        service = getattr(request.app.state, 'quote_service', None)
        if service is None:
            service = QuoteService(QuoteRepository(make_server_client(request.app.state.settings)))
            request.app.state.quote_service = service
        return service


@router.get('/{case_id}/quotes', response_model=Envelope[QuotesResponse],
            summary='API-13: 업무의 업체별 최신 견적·비용·체크·근거 조회',
            responses={404: {'description': 'RESOURCE_NOT_FOUND: 없음 또는 소유 범위 밖'}})
def quotes(case_id: UUID, principal=Depends(require_google_user), service=Depends(get_quote_service)):
    """Read only. One item per supplier (latest version_no); incumbent first, then supplier name.

    Amounts and quantities are Decimal strings without trailing zeros. An empty
    case returns items: []. tax_basis is top-level and not repeated in checks.
    """
    return api_json(service.get(principal.user_id, case_id).model_dump(mode='json'), headers=PRIVATE)
