"""Per-run tool scope. LLM arguments never define owner, batch or revision."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import time

from app.core.errors import ServiceError


@dataclass(repr=False)
class ExtractionScope:
    owner: str
    case_id: str
    revision: int
    batch_id: str
    mail_ids: tuple[str, ...]
    repo: object
    mailbox: object
    attachment_repo: object
    max_characters: int
    deadline: float
    lease_check: object = lambda: None
    blocked: bool = False
    job: dict | None = None
    agent_store: object = None
    full_quote_ids: list = field(default_factory=list)
    agent_metadata: dict = field(default_factory=dict)

    def remaining_seconds(self):
        self.lease_check()
        if self.blocked:
            raise ServiceError('EXTRACTION_DOCUMENT_LIMIT', '문서 처리 한도를 초과했습니다.', 503)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError('EXTRACTION_TIME_LIMIT', '추출 실행 한도를 초과했습니다.', 503)
        return remaining

    def check(self, case_id, expected_revision):
        self.remaining_seconds()
        if str(case_id) != self.case_id or expected_revision != self.revision:
            raise ServiceError('ACTION_NOT_ALLOWED', '도구 실행 범위를 벗어났습니다.', 409)
        return self.repo.context(self.owner, self.case_id, self.revision, self.batch_id)


CURRENT_SCOPE: ContextVar[ExtractionScope] = ContextVar('case_extraction_scope')
