"""Closed hold codes and server-owned Korean copy. Never forward model text."""
import re
from typing import Literal
from app.shared.domain.schemas import StrictModel

HoldCode = Literal['SUPPLIER_NOT_MATCHED', 'SUPPLIER_MISMATCH', 'MISSING_FIELD',
    'MISSING_EVIDENCE', 'SOURCE_READ_FAILED', 'NO_QUOTE_DATA', 'CONDITIONS_MISSING',
    'NO_SUITABLE_CANDIDATES', 'SEND_POLICY_REQUIRED', 'HOLD_REASON_UNAVAILABLE']
MESSAGES = {
    'SUPPLIER_NOT_MATCHED': '등록된 거래처와 연결되지 않아 견적을 저장하지 못했어요.',
    'SUPPLIER_MISMATCH': '메일에 연결된 거래처와 견적의 업체가 달라 확인이 필요해요.',
    'MISSING_FIELD': '견적을 저장하는 데 필요한 필수 정보가 없어 검토를 보류했어요.',
    'MISSING_EVIDENCE': '견적 정보의 원문 근거를 확인할 수 없어 검토를 보류했어요.',
    'SOURCE_READ_FAILED': '메일이나 첨부의 내용을 읽을 수 없어 검토를 보류했어요.',
    'NO_QUOTE_DATA': '메일에서 검토할 수 있는 견적 정보를 찾지 못했어요.',
    'CONDITIONS_MISSING': '검토에 필요한 요청 조건이 부족해 확인이 필요해요.',
    'NO_SUITABLE_CANDIDATES': '요청 조건에 맞는 거래처 후보를 찾지 못했어요.',
    'SEND_POLICY_REQUIRED': '견적 요청에 필요한 발송 설정을 확인해야 해요.',
    'HOLD_REASON_UNAVAILABLE': '보류 사유를 확인할 수 없어 담당자 확인이 필요해요.',
}
MISSING_MESSAGES = {
    'supplier_name': '업체명이 확인되지 않아 견적을 저장하지 못했어요.',
    'item_name': '품목명이 확인되지 않아 검토를 보류했어요.',
    'unit_price': '견적 단가가 확인되지 않아 검토를 보류했어요.',
    'unit': '견적의 단위가 확인되지 않아 검토를 보류했어요.',
    'quantity': '요청 수량이 확인되지 않아 검토를 보류했어요.',
}


class HoldInfo(StrictModel):
    code: HoldCode
    message: str


def hold_info(code, field=None):
    code = code if isinstance(code, str) and code in MESSAGES else 'HOLD_REASON_UNAVAILABLE'
    message = MISSING_MESSAGES.get(field, MESSAGES[code]) if code == 'MISSING_FIELD' and isinstance(field, str) else MESSAGES[code]
    return HoldInfo(code=code, message=message)


_ABSENT = r'(?:없(?!지(?:는)?\s*않|는\s*(?:것은|것이|건|게)\s*아니)|누락(?!되지\s*않)|부족(?!하지\s*않)|찾지\s*못|확인\s*(?:할\s*수\s*없|되지\s*않|되지\s*못|이\s*어려)|특정.{0,10}없|불명확)'
_FIELDS = {
    'supplier_name': r'(?:업체명|회사명|거래처명|공급사명|supplier_name)',
    'item_name': r'(?:품목명|item_name)', 'unit_price': r'(?:단가|unit_price)',
    'unit': r'(?:단위|\bunit\b)', 'quantity': r'(?:요청\s*수량|quantity)',
}


def _field(reason, explicit=None):
    if isinstance(explicit, str) and explicit in MISSING_MESSAGES:
        return explicit
    for field, pattern in _FIELDS.items():
        other_fields = '|'.join(value for key, value in _FIELDS.items() if key != field)
        gap = r'(?:(?!(?:' + other_fields + r')).){0,60}'
        if re.search(pattern + gap + _ABSENT, reason, re.S):
            return field
    return None


def from_agent_output(output, *, rejection_codes=()):
    """Prefer a fixed structured code; support old HOLD rows containing reason only.

    Free text is inspected only to select a closed code/template. Unrecognized or
    malformed data gets an honest fallback; no value is interpolated into copy.
    """
    if not isinstance(output, dict):
        return hold_info(None)
    verified = set(rejection_codes) & {'SUPPLIER_NOT_MATCHED', 'SUPPLIER_MISMATCH'}
    if verified:
        return hold_info(next(iter(verified)) if len(verified) == 1 else None)
    reason = output.get('reason')
    reason = reason[:4000] if isinstance(reason, str) else ''
    structured = output.get('hold')
    if (isinstance(structured, dict) and isinstance(structured.get('code'), str)
            and structured['code'] in MESSAGES):
        return hold_info(structured['code'], structured.get('field'))
    for key in ('hold_code', 'reason_code'):
        code = output.get(key)
        if isinstance(code, str) and code in MESSAGES:
            return hold_info(code, _field(reason))
    for code in MESSAGES:
        if code != 'HOLD_REASON_UNAVAILABLE' and re.search(r'(?<![A-Z0-9_])'+code+r'(?![A-Z0-9_])', reason):
            return hold_info(code, _field(reason))
    # Precise old supplier errors precede generic missing-information wording.
    if re.search(r'등록.{0,30}(?:거래처|업체|공급사).{0,35}(?:찾지\s*못|없|연결.{0,10}(?:않|없)|일치.{0,10}없)', reason, re.S):
        return hold_info('SUPPLIER_NOT_MATCHED')
    if re.search(r'후보.{0,40}' + _ABSENT, reason, re.S):
        return hold_info('NO_SUITABLE_CANDIDATES')
    field = _field(reason)
    if field is not None:
        return hold_info('MISSING_FIELD', field)
    if re.search(r'(?:필수|필요한).{0,50}(?:정보|항목|값).{0,30}'+_ABSENT, reason, re.S):
        return hold_info('MISSING_FIELD')
    if re.search(r'(?:첨부|원문|문서).{0,30}(?:읽을\s*수\s*없|읽지\s*못|읽기\s*실패)', reason, re.S):
        return hold_info('SOURCE_READ_FAILED')
    if re.search(r'견적.{0,20}(?:정보|내용).{0,20}(?:없|찾지\s*못)', reason, re.S):
        return hold_info('NO_QUOTE_DATA')
    return hold_info(None)


def is_hold_output(output):
    if not isinstance(output, dict):
        return False
    if output.get('next_action') == 'HOLD' or output.get('status') == 'ON_HOLD':
        return True
    steps = output.get('steps')
    return (isinstance(steps, list) and bool(steps) and isinstance(steps[-1], dict)
            and steps[-1].get('next_action') == 'HOLD')
