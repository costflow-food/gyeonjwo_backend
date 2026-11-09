"""API-10: owner-scoped sent/received mail timeline of one work case plus reply_summary.

Read-only. No body text, attachment contents, provider IDs or tokens; the original
mail stays behind API-25. Ordering is sent_or_received_at DESC, mail_id DESC (08 §1).
"""
from datetime import datetime, timezone
from uuid import UUID

from app.core.errors import ServiceError
from app.features.work_cases.application.cursor import iso_z, utc_time

MESSAGE_COLUMNS = 'id,direction,subject,sender,recipients_jsonb,sent_or_received_at,attachments_jsonb,supplier_id,mail_classification'
DIRECTIONS = frozenset({'IN', 'OUT'})


def public_message(row):
    direction = row.get('direction')
    if direction not in DIRECTIONS:
        raise ValueError('Invalid mail direction')
    recipients = row.get('recipients_jsonb') or []
    attachments = row.get('attachments_jsonb') or []
    if not isinstance(recipients, list) or not all(isinstance(value, str) for value in recipients):
        raise ValueError('Invalid recipients')
    if not isinstance(attachments, list):
        raise ValueError('Invalid attachments')
    supplier_id = row.get('supplier_id')
    return {
        'mail_id': str(UUID(row['id'])),
        'direction': direction,
        'subject': row.get('subject') or '',
        'sender': row.get('sender') or '',
        'recipients': recipients,
        'sent_or_received_at': iso_z(utc_time(row['sent_or_received_at'])),
        'supplier_id': str(UUID(supplier_id)) if supplier_id else None,
        'mail_classification': row.get('mail_classification'),
        'attachment_count': len(attachments),
    }


def list_case_messages(client, codec, owner, case_id, *, cursor=None, limit=25, reply_summaries=None):
    case_id = str(UUID(str(case_id)))
    scope = codec.scope(owner, 'work-case-messages', None, case_id)
    snapshot, after = (codec.decode(cursor, scope) if cursor is not None
                       else (iso_z(datetime.now(timezone.utc)), None))
    try:
        cases = (client.table('work_cases').select('id').eq('owner_user_id', str(owner))
                 .eq('id', case_id).limit(1).execute().data)
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무 메일을 조회할 수 없습니다.', 503) from None
    if not isinstance(cases, list):
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무 메일을 조회할 수 없습니다.', 503)
    if not cases:
        raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
    try:
        query = (client.table('mail_messages').select(MESSAGE_COLUMNS)
                 .eq('owner_user_id', str(owner)).eq('work_case_id', case_id)
                 .lte('sent_or_received_at', snapshot))
        if after:
            timestamp, mail_id = after
            query = query.or_(f'sent_or_received_at.lt.{timestamp},'
                              f'and(sent_or_received_at.eq.{timestamp},id.lt.{mail_id})')
        rows = (query.order('sent_or_received_at', desc=True).order('id', desc=True)
                .limit(limit + 1).execute().data)
        if not isinstance(rows, list):
            raise ValueError('Invalid mail results')
        items = [public_message(row) for row in rows[:limit]]
    except Exception:
        # Stored rows can contain mail content; never echo storage errors.
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무 메일을 조회할 수 없습니다.', 503) from None
    next_cursor = (codec.encode(scope, snapshot, (items[-1]['sent_or_received_at'], items[-1]['mail_id']))
                   if len(rows) > limit else None)
    summary = None
    if reply_summaries is not None:
        try:
            summary = reply_summaries(owner, [case_id]).get(case_id)
        except ServiceError as error:
            # Unavailable reply facts are unknown (null), never a pretend 0/0.
            if error.code != 'PROCESSING_UNAVAILABLE':
                raise
    return {'case_id': case_id, 'items': items, 'next_cursor': next_cursor,
            'reply_summary': summary.model_dump(mode='json') if summary is not None else None}
