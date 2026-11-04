from pydantic import ValidationError
from app.core.errors import ServiceError
from app.features.work_cases.domain.contact_candidates import ContactCandidatesResponse


class ContactCandidateService:
    def __init__(self, repo):
        self.repo = repo

    def get(self, owner, case_id, expected_revision=None):
        revision = self.repo.case_revision(owner, case_id)
        if expected_revision is not None and expected_revision != revision:
            raise ServiceError('REVISION_CONFLICT', '업무 입력이 바뀌었습니다. 최신 내용을 확인해 주세요.',
                               409, current_revision=revision, retryable=False)
        rows = self.repo.list(owner, case_id, revision)
        try:
            return ContactCandidatesResponse(case_id=case_id, revision=revision, items=rows)
        except ValidationError:
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 연락처 후보 형식을 확인할 수 없습니다.', 503) from None
