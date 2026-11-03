from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.candidates import CandidateService
from app.features.work_cases.infrastructure.candidate_repository import CandidateRepository
from app.features.work_cases.domain.candidates import CandidatesResponse, CandidateSelectionRequest, CandidateSelectionResponse
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1/work-cases', tags=['Case candidates'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def get_candidate_service(request: Request):
    with request.app.state.service_lock:
        service = getattr(request.app.state, 'candidate_service', None)
        if service is None:
            service = CandidateService(CandidateRepository(make_server_client(request.app.state.settings)))
            request.app.state.candidate_service = service
        return service


@router.get('/{case_id}/candidates', response_model=Envelope[CandidatesResponse],
            summary='API-11: 저장된 업무별 후보·근거·체크·선택 조회')
def candidates(case_id: UUID, principal=Depends(require_google_user), service=Depends(get_candidate_service)):
    """Read only; never starts an agent or creates drafts. Stale sets remain marked.

    checks.status is MET/UNMET/UNKNOWN to match the frontend. Exact preference
    scores are preserved; an integer convenience field is never rounded.
    """
    return api_json(service.get(principal.user_id, case_id).model_dump(mode='json'), headers=PRIVATE)


@router.put('/{case_id}/candidate-selection', response_model=Envelope[CandidateSelectionResponse],
            summary='API-12: 업무별 요청 대상 선택·선택 업체 RFQ 초안 공개')
def select_candidates(case_id: UUID, payload: CandidateSelectionRequest, request: Request,
                      principal=Depends(require_google_user), service=Depends(get_candidate_service),
                      idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    """200 confirms selection/draft creation, never actual Gmail delivery.

    Approval ON waits for API-19. OFF queues only selected, policy-authorized,
    uncautioned targets. Existing mailbox limits are preserved pending decision.
    """
    settings = request.app.state.settings
    worker = getattr(request.app.state, 'rfq_workflow_worker', None)
    ready = (settings.rfq_workflow_enabled and settings.rfq_mail_sending_enabled and worker is not None
             and worker.running and str(principal.user_id) in {str(value) for value in settings.rfq_workflow_owner_ids})
    response = service.submit(principal.user_id, case_id, payload, idempotency_key, dispatch_ready=bool(ready))
    if worker is not None:
        worker.wake.set()
    return JSONResponse(response, headers={**PRIVATE, 'X-Request-ID': response['meta']['request_id']})
