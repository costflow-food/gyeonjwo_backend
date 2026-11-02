"""Count suppliers, never messages; only verified current-round evidence counts."""
from decimal import Decimal, localcontext
from uuid import UUID

from app.features.work_cases.domain.listing import ReplySummary


def reply_summary(sent_supplier_ids, valid_reply_supplier_ids):
    if not isinstance(sent_supplier_ids, list) or not isinstance(valid_reply_supplier_ids, list):
        raise ValueError('Invalid reply evidence')
    sent = {str(UUID(value)) for value in sent_supplier_ids}
    replied = {str(UUID(value)) for value in valid_reply_supplier_ids}
    requested, responded = len(sent), len(sent & replied)
    with localcontext() as context:
        context.prec = 28
        rate = format(Decimal(responded) / Decimal(requested), 'f') if requested else None
    return ReplySummary(requested_supplier_count=requested,
                        responded_supplier_count=responded, reply_rate=rate)
