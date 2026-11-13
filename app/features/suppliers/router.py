from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from app.api.responses import api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.suppliers.registration import SupplierRegistrationInput
from app.features.suppliers.repository import SupplierRepository
from app.features.work_cases.application.cursor import CursorCodec
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1', tags=['Suppliers'])


def supplier_repository(request: Request, principal=Depends(require_google_user)):
    return SupplierRepository(make_server_client(request.app.state.settings), principal.user_id)


@router.get('/suppliers', summary='거래처 목록')
def suppliers(request: Request, repo=Depends(supplier_repository),
              q: Annotated[str, Query(max_length=200)] = '',
              cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 25):
    key = request.app.state.settings.supabase_secret_key.get_secret_value()
    return api_json(repo.list(CursorCodec(key), q, cursor, limit))


@router.post('/suppliers', status_code=201, summary='거래처 수동 등록')
def register_supplier(body: SupplierRegistrationInput, repo=Depends(supplier_repository)):
    return api_json(repo.register(body.model_dump()), status=201)


@router.get('/suppliers/{supplier_id}', summary='거래처 상세')
def supplier(supplier_id: UUID, repo=Depends(supplier_repository)):
    return api_json(repo.detail(str(supplier_id)))


@router.get('/supplier-items/{supplier_item_id}/price-history', summary='품목별 가격 이력')
def supplier_price_history(supplier_item_id: UUID, repo=Depends(supplier_repository)):
    return api_json(repo.history(str(supplier_item_id)))
