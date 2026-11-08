"""API-14: stored review card -> frontend card shape.

The card is written by the DB reply review (commit_reply_review) after review_card_check:
plans carry plan_id from the RESPONSE_PLAN output, and review_outcome/closure_reason are
fixed by the DB. Allocations hold quote_version_id only, so supplier names, unit and
unit price are filled here from suppliers/quote_versions.
"""
from app.core.errors import ServiceError
from app.features.work_cases.domain.quotes import decimal_text
from app.features.work_cases.domain.review_card import REVIEW_ACTIONS, ReviewCardResponse

UNKNOWN_SUPPLIER = '이름 미확인 업체'
CLOSED_DECISIONS = {'CONFIRM_REVIEW', 'ACCEPT_INCREASE'}


def review_actions(case, plans, is_stale, changes_ready):
    """허용·비활성 판단 동작. 최신 카드이고 업무가 검토 완료 상태이며 판단 저장(API-15) 함수가 배포됐을 때만 판단할 수 있다."""
    decision = case.get('decision')
    if case.get('archived_at') or case.get('accepted_at') or decision in CLOSED_DECISIONS:
        return [], []
    if is_stale:
        return [], [{'action': action, 'reason_code': 'REVIEW_STALE',
                     'message': '새 견적이 반영되지 않은 카드입니다. 다시 검토한 카드에서 판단해 주세요.'}
                    for action in REVIEW_ACTIONS]
    if case.get('status') != 'Complete':
        return [], [{'action': action, 'reason_code': 'REVIEW_NOT_CURRENT',
                     'message': '검토가 진행 중이거나 보류된 업무입니다.'} for action in REVIEW_ACTIONS]
    if not changes_ready:
        return [], [{'action': action, 'reason_code': 'CASE_CHANGE_INTEGRATION_REQUIRED',
                     'message': '업무 변경 기능이 아직 준비되지 않았습니다.'} for action in REVIEW_ACTIONS]
    allowed, disabled = [], []
    if any(plan['validation_status'] == 'VALID' and plan['plan_id'] for plan in plans):
        allowed.append('SELECT_PLAN')
    else:
        disabled.append({'action': 'SELECT_PLAN', 'reason_code': 'NO_SELECTABLE_PLAN',
                         'message': '확정 가능한 구매안이 없습니다.'})
    if decision != 'HOLD':
        allowed.append('HOLD')
    allowed.append('CONFIRM_REVIEW')
    return allowed, disabled


def card_decision(case):
    """업무에 저장된 판단. 아직 없으면 None."""
    decision = case.get('decision')
    if decision is None:
        return None
    selected = case.get('selected_plan_jsonb') if isinstance(case.get('selected_plan_jsonb'), dict) else {}
    return {'decision': decision, 'plan_id': case.get('selected_plan_id'),
            'note': selected.get('note'), 'decided_at': case.get('selected_at')}


class ReviewCardService:
    def __init__(self, repo, market=None):
        self.repo = repo
        self.market = market

    def get(self, owner, case_id, changes_ready=False):
        """업무의 최신 검토 카드. 카드가 없으면 404 REVIEW_NOT_READY. changes_ready는 판단 저장 함수 배포 여부."""
        case = self.repo.case(owner, case_id)
        card = case.get('latest_review_jsonb')
        if card is None:
            raise ServiceError('REVIEW_NOT_READY', '검토 카드가 아직 준비되지 않았습니다.', 404)
        try:
            return self.build(owner, case, card, changes_ready)
        except ServiceError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 검토 카드 형식을 확인할 수 없습니다.', 503) from None

    def build(self, owner, case, card, changes_ready=False):
        """저장된 카드에 업체 이름·단가·판단·동작을 채워 응답으로 만든다."""
        if not isinstance(card, dict):
            raise ValueError('Invalid card')
        plans = card.get('plans') or []
        counter = card.get('counter_offer')
        questions = card.get('questions') or []
        quote_ids = {str(a.get('quote_id') or a['quote_version_id']) for plan in plans for a in plan['allocations']}
        quotes = self.repo.quotes(owner, case['id'], quote_ids)
        supplier_ids = ({str(a['supplier_id']) for plan in plans for a in plan['allocations']}
                        | {str(q['supplier_id']) for q in questions}
                        | ({str(counter['target']['supplier_id'])} if counter and counter.get('target') else set()))
        names = self.repo.suppliers(owner, supplier_ids)
        revision = case['review_revision']

        def name(supplier_id, stored=None):
            """카드에 고정된 이름이 있으면 그대로, 없으면 현재 업체 이름."""
            return stored or names.get(str(supplier_id)) or UNKNOWN_SUPPLIER

        def allocation(item):
            """배분 한 줄. 단위·단가가 카드에 없으면 견적에서 채운다."""
            quote_id = str(item.get('quote_id') or item['quote_version_id'])
            quote = quotes.get(quote_id, {})
            return {'supplier_id': item['supplier_id'], 'supplier_name': name(item['supplier_id'], item.get('supplier_name')),
                    'quote_id': quote_id, 'quantity': decimal_text(item['quantity']),
                    'unit': item.get('unit') or quote.get('unit'),
                    'unit_price': decimal_text(item.get('unit_price') or quote.get('unit_price'))}

        response_plans = [{'plan_id': plan.get('plan_id'), 'plan_type': plan['plan_type'],
                           'validation_status': plan.get('validation_status') or 'VALID',
                           'total_amount': decimal_text(plan.get('total_amount')), 'currency': plan.get('currency') or 'KRW',
                           'allocations': [allocation(item) for item in plan['allocations']],
                           'conditions': plan.get('conditions') or [], 'adjustments': plan.get('adjustments') or [],
                           'reason': plan.get('reason')} for plan in plans]
        response_counter = None
        if counter:
            target, reference = counter.get('target'), counter.get('reference')
            response_counter = {
                'status': counter['status'], 'reason': counter.get('reason'), 'draft_ids': counter.get('draft_ids') or [],
                'target': {'supplier_id': target['supplier_id'],
                           'supplier_name': name(target['supplier_id'], target.get('supplier_name')),
                           'target_unit_price': decimal_text(target['target_unit_price']),
                           'conditions': target.get('conditions') or []} if target else None,
                'reference': {**{key: reference.get(key) for key in ('status', 'unit')},
                              **{key: decimal_text(reference.get(key)) for key in (
                                  'requested_unit_price', 'previous_unit_price', 'ceiling_unit_price', 'alternative_total')},
                              'allowed_range': [decimal_text(value) for value in reference.get('allowed_range') or []]}
                if reference else None}
        is_stale = card['input_revision'] < case['input_revision']
        allowed, disabled = review_actions(case, response_plans, is_stale, changes_ready)
        return ReviewCardResponse(
            case_id=case['id'], review_revision=revision, input_revision=card['input_revision'], is_stale=is_stale,
            created_at=card.get('created_at'),
            source_quote_ids=card.get('source_quote_ids') or card.get('quote_version_ids') or [],
            review_outcome=card['review_outcome'], closure_reason=card.get('closure_reason'),
            plans=response_plans, counter_offer=response_counter,
            questions=[{'supplier_id': q['supplier_id'], 'supplier_name': name(q['supplier_id'], q.get('supplier_name')),
                        'field': q['field'], 'priority': q['priority'], 'reason': q['reason'],
                        'break_even_amount': decimal_text(q.get('break_even_amount')), 'formula': q.get('formula')}
                       for q in questions],
            unresolved=card.get('unresolved') or [], decision=card_decision(case),
            allowed_actions=allowed, disabled_actions=disabled,
            market_reference=self.market.get(case, quotes) if self.market else None)
