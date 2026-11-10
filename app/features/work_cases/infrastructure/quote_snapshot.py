"""Decode immutable quotes without inventing fields or trusting conflicting snapshots."""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID
from app.core.errors import ServiceError

QUOTE_COLUMNS = ('id,work_case_id,supplier_id,source_mail_id,version_no,supersedes_id,unit_price::text,unit,'
    'shipping_cost::text,available_quantity::text,effective_from,valid_until,checks_jsonb,'
    'pricing_terms_jsonb,cost_terms_jsonb,item_notes_jsonb,evidence_jsonb,extraction_status,currency')


def decode_quote(row):
    from app.agents.tools import QuoteVersionRecord
    try:
        raw = deepcopy(row)
        quote_id = str(UUID(raw.pop('id')))
        saved = raw['pricing_terms_jsonb'].get('agent_record')
        if saved is None:
            for key in ('cost_terms_jsonb', 'item_notes_jsonb'):
                value = raw[key]
                if isinstance(value, dict):
                    if value and 'items' not in value:
                        raise ValueError('Unsupported quote collection shape')
                    raw[key] = value.get('items', [])
            raw.update(mail_normalized={}, case_conditions=None)
            record = QuoteVersionRecord.model_validate(raw)
        else:
            record = QuoteVersionRecord.model_validate(saved)
            # A stale agent_record must not override the row's actual price,
            # quantity, identity or validity. Compare numbers without rounding.
            for key in ('work_case_id','supplier_id','source_mail_id','version_no','supersedes_id',
                        'unit','effective_from','valid_until','extraction_status','currency'):
                if getattr(record, key) != row[key]:
                    raise ValueError('Conflicting quote snapshot')
            for key in ('unit_price','shipping_cost','available_quantity'):
                left, right = getattr(record, key), row[key]
                if (left is None) != (right is None):
                    raise ValueError('Conflicting unknown quote amount')
                if left is not None and (not Decimal(str(right)).is_finite() or Decimal(left) != Decimal(str(right))):
                    raise ValueError('Conflicting quote amount')
        return quote_id, record
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
        raise ServiceError('QUOTE_CONTRACT_REQUIRED', '견적 저장 형식과 원문 근거를 확인해야 합니다.', 503) from None
