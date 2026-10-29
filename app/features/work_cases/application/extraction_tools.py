"""Per-run tool scope. LLM arguments never define owner, batch or revision."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import time

from app.core.errors import ServiceError
from app.features.mail.application.attachments import fetch_gmail_attachment
from app.features.review.infrastructure.documents import read_pdf
from app.features.work_cases.domain.extraction import ExtractionResult


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
    evidence_texts: dict = field(default_factory=dict)
    result: ExtractionResult | None = None

    def check(self, case_id, expected_revision):
        self.lease_check()
        if time.monotonic() >= self.deadline:
            raise ServiceError('EXTRACTION_TIME_LIMIT', '추출 실행 한도를 초과했습니다.', 503)
        if str(case_id) != self.case_id or expected_revision != self.revision:
            raise ServiceError('ACTION_NOT_ALLOWED', '도구 실행 범위를 벗어났습니다.', 409)
        return self.repo.context(self.owner, self.case_id, self.revision, self.batch_id)


CURRENT_SCOPE: ContextVar[ExtractionScope] = ContextVar('case_extraction_scope')


def get_scope(case_id, expected_revision):
    scope = CURRENT_SCOPE.get()
    scope.check(case_id, expected_revision)
    return scope


def context(case_id, expected_revision):
    scope = get_scope(case_id, expected_revision)
    row = scope.repo.context(scope.owner, scope.case_id, scope.revision, scope.batch_id)
    return {key: row.get(key) for key in ('id', 'input_revision', 'item_name', 'specification',
                                        'quantity', 'unit', 'policy_jsonb')} | {'mail_ids': list(scope.mail_ids)}


def evidence(case_id, expected_revision, mail_id):
    scope = get_scope(case_id, expected_revision)
    if mail_id not in scope.mail_ids:
        raise ServiceError('RESOURCE_NOT_FOUND', '실행 범위의 메일이 아닙니다.', 404)
    mail = scope.repo.mail(scope.owner, scope.case_id, mail_id)
    sources = [{'evidence_id': f'{mail_id}:body', 'text': mail.get('body_text') or '', 'status': 'READABLE'},
               {'evidence_id': f'{mail_id}:subject', 'text': mail.get('subject') or '', 'status': 'READABLE'}]
    for attachment in mail.get('attachments_jsonb') or []:
        ref = attachment.get('provider_part_ref', attachment.get('part_id'))
        source = {'evidence_id': f'{mail_id}:part:{ref}', 'status': 'UNSUPPORTED', 'text': ''}
        if attachment.get('mime_type') == 'application/pdf' and isinstance(ref, str):
            # This is the same owner-scoped retrieval function as API-26.
            try:
                content = fetch_gmail_attachment(scope.attachment_repo, scope.mailbox,
                                                scope.owner, mail_id, ref)
                text = read_pdf(content.content)
                source.update(status='READABLE' if text.strip() else 'OCR_REQUIRED', text=text)
            except ValueError:
                source['status'] = 'UNREADABLE'
            except ServiceError as error:
                if error.code == 'MAILBOX_RECONNECT_REQUIRED':
                    raise
                source['status'] = 'UNREADABLE'
        sources.append(source)
    for source in sources:
        if source['status'] == 'READABLE':
            scope.evidence_texts[source['evidence_id']] = source['text']
    if sum(map(len, scope.evidence_texts.values())) > scope.max_characters:
        raise ServiceError('EXTRACTION_DOCUMENT_LIMIT', '문서 처리 한도를 초과했습니다.', 503)
    scope.check(case_id, expected_revision)
    return {'sources': sources}


def stage(case_id, expected_revision, result):
    scope = get_scope(case_id, expected_revision)
    # No partial writes during the model/tool loop. The validated final result,
    # quote(s), work-case fields, run and log are committed together afterwards.
    scope.result = ExtractionResult.model_validate(result).validated(scope.evidence_texts)
    return {'staged': True, 'persisted': False, 'unknown_fields': scope.result.unknown_fields}
