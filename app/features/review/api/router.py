from typing import Annotated
from urllib.parse import quote
from fastapi import APIRouter, Body, Depends, Path
from fastapi.responses import Response
from app.api.dependencies import get_service, require_member
from app.api.responses import Envelope, ErrorEnvelope, api_json
from app.shared.domain.schemas import EmptyRequest
from app.features.review.domain.schemas import NotesRequest
from app.features.review.domain.responses import AnalysisResponse, FileListResponse, NotesResponse, QueueChangeResponse, WorkDetailResponse

router = APIRouter(tags=['review'])
protected = [Depends(require_member)]
WorkId = Annotated[str, Path(pattern=r'^W-[0-9]{8}-[0-9]{3,}$', max_length=64)]

@router.get('/api/v1/work-items/{work_id}', dependencies=protected, response_model=Envelope[WorkDetailResponse])
def detail(work_id: WorkId, service=Depends(get_service)):
    return api_json(service.detail(work_id), model=WorkDetailResponse)


@router.post('/api/v1/work-items/{work_id}/reclassify', dependencies=protected, response_model=Envelope[QueueChangeResponse])
def reclassify(work_id: WorkId, payload: EmptyRequest = Body(default_factory=EmptyRequest), service=Depends(get_service)):
    return api_json(service.reclassify(work_id), model=QueueChangeResponse)


@router.post('/api/v1/work-items/{work_id}/analyze', dependencies=protected, response_model=Envelope[AnalysisResponse],
          responses={502: {'model': Envelope[AnalysisResponse]}, 503: {'model': ErrorEnvelope}})
def analyze(work_id: WorkId, payload: EmptyRequest = Body(default_factory=EmptyRequest), service=Depends(get_service)):
    status, result = service.analyze(work_id)
    return api_json(result, status, model=AnalysisResponse)


@router.post('/api/v1/work-items/{work_id}/skip', dependencies=protected, response_model=Envelope[QueueChangeResponse])
def skip(work_id: WorkId, payload: EmptyRequest = Body(default_factory=EmptyRequest), service=Depends(get_service)):
    return api_json(service.skip(work_id), model=QueueChangeResponse)


@router.get('/api/v1/work-items/{work_id}/files', dependencies=protected, response_model=Envelope[FileListResponse])
def files(work_id: WorkId, service=Depends(get_service)):
    return api_json(service.files(work_id), model=FileListResponse)


@router.get('/api/v1/work-items/{work_id}/files/{filename}', dependencies=protected)
def file(work_id: WorkId, filename: str, service=Depends(get_service)):
    meta, content = service.file(work_id, filename)
    return Response(content, media_type=meta['content_type'], headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(meta['original_name'], safe=''),
        'Content-Security-Policy': "sandbox; default-src 'none'"})


@router.get('/api/v1/work-items/{work_id}/reference-files', dependencies=protected)
def reference_files(work_id: WorkId, service=Depends(get_service)):
    return api_json(service.reference_files(work_id))


@router.get('/api/v1/work-items/{work_id}/reference-files/{file_id}', dependencies=protected)
def reference_file(work_id: WorkId, file_id: Annotated[str, Path(pattern='^[a-f0-9]{64}$')], service=Depends(get_service)):
    meta, content = service.reference_file(work_id, file_id)
    return Response(content, media_type=meta['content_type'], headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(meta['original_name'], safe=''),
        'Content-Security-Policy': "sandbox; default-src 'none'"})


@router.patch('/api/v1/work-items/{work_id}/notes', dependencies=protected, response_model=Envelope[NotesResponse])
def notes(work_id: WorkId, payload: NotesRequest, service=Depends(get_service)):
    return api_json(service.notes(work_id, payload.notes), model=NotesResponse)

