"""Owner-scoped, read-only workflow audit listing."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.responses import api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.cursor import CursorCodec
from app.features.workflow_logs.repository import list_workflow_logs
from app.shared.domain.schemas import StrictModel
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1', tags=['Workflow logs'])

class WorkflowLogQuery(StrictModel):
    case_id: UUID | None = None
    cursor: str | None = None
    limit: int = Query(default=25, ge=1, le=100)


@router.get('/workflow-logs', summary='업무 로그 조회')
def workflow_logs(request: Request, filters: Annotated[WorkflowLogQuery, Query()],
                  principal=Depends(require_google_user)):
    settings = request.app.state.settings
    codec = CursorCodec(settings.supabase_secret_key.get_secret_value())
    return api_json(list_workflow_logs(make_server_client(settings), codec, principal.user_id, filters),
                    headers={'Cache-Control': 'private, no-store'})
