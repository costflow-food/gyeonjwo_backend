from uuid import UUID
from fastapi import APIRouter, Depends, Request
from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.review_card import ReviewCardService
from app.features.work_cases.domain.review_card import ReviewCardResponse
from app.features.work_cases.infrastructure.review_card_repository import ReviewCardRepository
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1/work-cases', tags=['Case review card'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def get_review_card_service(request: Request):
    """요청마다 같은 검토 카드 조회 서비스를 쓴다."""
    with request.app.state.service_lock:
        service = getattr(request.app.state, 'review_card_service', None)
        if service is None:
            service = ReviewCardService(ReviewCardRepository(make_server_client(request.app.state.settings)))
            request.app.state.review_card_service = service
        return service


@router.get('/{case_id}/review-card', response_model=Envelope[ReviewCardResponse],
            summary='API-14: 업무의 최신 검토 카드(구매안·역제안·확인 항목·판단·동작) 조회',
            responses={404: {'description': 'RESOURCE_NOT_FOUND: 없음 또는 소유 범위 밖 / REVIEW_NOT_READY: 카드 없음'}})
def review_card(case_id: UUID, principal=Depends(require_google_user), service=Depends(get_review_card_service)):
    """Read only. is_stale when the card's input_revision is older than the case's.

    Actions are allowed only for the current card of a Complete case; decisions are
    saved by API-15 (separate ticket).
    """
    return api_json(service.get(principal.user_id, case_id).model_dump(mode='json'), headers=PRIVATE)
