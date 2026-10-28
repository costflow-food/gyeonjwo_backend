import calendar
import json
import re
import time
from datetime import date
from decimal import Decimal

from app.features.review.domain.calculations import ZERO, all_rounds_match, evaluate
from app.features.review.infrastructure.documents import document_text, read_pdf
from app.core.errors import ReviewRequired, ServiceError
from app.features.review.domain.schemas import CalculationProposal, CardTexts, ComparisonVerdict, Extraction, ReviewSummary, Verdict
from app.features.review.domain.validation import decimal_value, normalize_extraction, unit_name
from app.shared.infrastructure.llm import json_default
from app.features.review.domain.responses import AnalysisResponse


def as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def cost_value(cost, quantity, start, end):
    if cost['payer'] == 'SUPPLIER' or cost['included_in_price'] == 'TRUE':
        return ZERO
    if cost['payer'] != 'BUYER' or cost['included_in_price'] != 'FALSE' or cost['amount'] is None:
        return None
    if cost['basis'] == 'PER_UNIT':
        if cost['coverage_from'] and not (as_date(cost['coverage_from']) <= start <= end <= as_date(cost['coverage_to'])):
            return None
        if cost.get('coverage_quantity') is not None and cost['coverage_quantity'] != quantity:
            return None
        return cost['amount'] * quantity
    if cost['basis'] == 'MONTHLY_FIXED':
        if (cost['coverage_from'] and cost['coverage_to'] and cost['coverage_quantity'] == quantity
                and as_date(cost['coverage_from']) == start and as_date(cost['coverage_to']) == end):
            return cost['amount']
    return None


def paired_costs(costs, quantity, start, end):
    result = {}
    for kind in ('TRANSPORT', 'OTHER_LOGISTICS'):
        matches = [cost for cost in costs if cost['cost_type'] == kind]
        result[kind] = cost_value(matches[0], quantity, start, end) if len(matches) == 1 else None
    return result


def request_costs(fields, baseline, quantity, start, end):
    basis = fields.get('cost_basis') or ''
    compact_basis = re.sub(r'\s+', '', basis)
    payer = fields.get('cost_payer') or ''
    inclusion = fields.get('cost_included_in_price') or ''
    # Only an explicit equality statement permits inheritance of verified contract costs.
    same = re.search(r'기존(?:계약|비용|조건)?(?:과|와)?동일(?:함|합니다)?', compact_basis)
    negated = re.search(r'(?:동일하지|동일하진|동일한것이아니|다름|상이|아니)', compact_basis)
    if same and not negated:
        if any(word in compact_basis for word in ('미확인', '추후', '별도확인', '일부', '만동일')):
            raise ReviewRequired('COST_UNCONFIRMED', '일부 비용만 동일하거나 미확인인 설명으로 전체 비용을 확정하지 않습니다.')
        scoped_transport = '운송비' in compact_basis
        scoped_other = '기타물류비' in compact_basis
        if scoped_transport != scoped_other:
            raise ReviewRequired('COST_UNCONFIRMED', '한 종류의 비용에만 해당하는 동일 조건은 전체 비용으로 확장하지 않습니다.')
        if (payer in ('SUPPLIER', '공급사 부담', '거래처 부담') or inclusion in ('TRUE', '포함', '단가 포함')) and any(baseline.values()):
            raise ReviewRequired('COST_CONFLICT', '기존 비용과 동일하다는 설명과 부담·포함 조건이 충돌합니다.')
        for name, kind in [('transport_amount', 'TRANSPORT'), ('other_logistics_amount', 'OTHER_LOGISTICS')]:
            if fields.get(name) is not None and decimal_value(fields[name], name) != baseline[kind]:
                raise ReviewRequired('COST_CONFLICT', '기존과 동일하다는 비용 설명과 금액이 다릅니다.', name)
        return baseline
    if payer in ('SUPPLIER', '공급사 부담', '거래처 부담') or inclusion in ('TRUE', '포함', '단가 포함'):
        return {'TRANSPORT': ZERO, 'OTHER_LOGISTICS': ZERO}
    if payer not in ('BUYER', '구매사 부담', '구매사(귀사) 부담', '구매자 부담', '귀사 부담') or inclusion not in ('FALSE', '별도', '단가 별도', '미포함'):
        raise ReviewRequired('COST_UNCONFIRMED', '요청 비용의 부담 주체·단가 포함 여부를 확인할 수 없습니다.')
    result = {}
    for field, kind in [('transport_amount', 'TRANSPORT'), ('other_logistics_amount', 'OTHER_LOGISTICS')]:
        amount = decimal_value(fields.get(field), field)
        if basis in ('PER_UNIT', 'kg당', '단위당'):
            result[kind] = amount * quantity
        elif '월정액' in basis:
            if re.search(r'[0-9]+\s*일', basis):
                raise ReviewRequired('COST_PERIOD_UNKNOWN', '월정액의 일부 날짜 범위는 자동 배분하지 않습니다.')
            months = re.findall(r'([0-9]{4})(?:년\s*|-)([0-9]{1,2})(?:월|(?=\s|$))', basis)
            if not months or {(int(year), int(month)) for year, month in months} != {(start.year, start.month)}:
                raise ReviewRequired('COST_PERIOD_UNKNOWN', '요청 월정액의 적용 연·월이 검토 기간과 다릅니다.')
            # Exact coverage quantity must be stated; no proportional allocation.
            compact = basis.replace(',', '').replace(' ', '')
            quantities = [Decimal(value) for value in re.findall(r'([0-9]+(?:\.[0-9]+)?)(?:kg|KG|㎏)', compact)]
            if quantities != [quantity]:
                raise ReviewRequired('COST_COVERAGE_UNKNOWN', '요청 월정액의 대상 물량이 확인되지 않았습니다.')
            result[kind] = amount
        else:
            raise ReviewRequired('COST_BASIS_UNKNOWN', '요청 비용의 기간·산정 기준을 확인할 수 없습니다.')
    return result


class AnalysisPipeline:
    def __init__(self, settings, agent, references, storage, client):
        self.settings, self.agent, self.references, self.storage, self.client = settings, agent, references, storage, client

    def _verify(self, role, data, deadline, output_failure=False):
        verdict = self.agent.run(role, 'Independently verify against original source and reference data. '
                                 'Return passed=false for missing/contradictory evidence. Do not repair values or calculate money. '
                                 'For comparison verification, judge the supplied exclusion decisions, not completeness of every candidate. '
                                 'Reason in Korean; cite only supplied sources.', data, Verdict, deadline)
        if not verdict.passed:
            if output_failure:
                raise ServiceError('OUTPUT_VALIDATION_FAILED', '카드 출력 검증에 실패했습니다.', 502)
            raise ReviewRequired('VALIDATION_FAILED', verdict.reason, ', '.join(verdict.sources))

    def _reference_texts(self, refs):
        result = {}
        for ref in sorted(set(refs)):
            path = ref.split('#', 1)[0]
            if not path or path.startswith('/') or '\\' in path or '://' in path or any(part in ('', '.', '..') for part in path.split('/')):
                raise ServiceError('INVALID_REFERENCE_PATH', '기준자료 원문 경로가 올바르지 않습니다.', 502)
            try:
                content = self.client.storage.from_('gyeonjwo-reference-files').download(path)
                if len(content) > 52_428_800:
                    raise ValueError('Reference too large')
                if path.lower().endswith('.pdf'):
                    text = read_pdf(content)
                elif path.lower().endswith(('.txt', '.csv')):
                    text = content.decode('utf-8')
                else:
                    raise ValueError('Unsupported reference format')
                result[ref] = text
            except Exception:
                raise ServiceError('SOURCE_READ_FAILED', '기준자료 원문을 읽을 수 없습니다.', 502) from None
            if sum(map(len, result.values())) > self.settings.max_document_characters:
                raise ReviewRequired('REFERENCE_TOO_LARGE', '기준자료 원문이 분석 범위를 초과했습니다.')
        return result

    def run(self, work_id, source, on_item, lease):
        deadline = time.monotonic() + self.settings.analysis_timeout_seconds
        document, sections = document_text(work_id, source, self.storage, self.settings.max_document_characters, with_sections=True)
        extraction = self.agent.run('extraction', 'Extract exactly these 20 fields as verbatim substrings with source locations: '
                                    'supplier_name,item_name,order_unit,net_kg_per_order_unit,origin,grade,processing,'
                                    'old_unit_price,new_unit_price,unit,currency,effective_date,reason,requested_quantity,'
                                    'requested_delivery_date,transport_amount,other_logistics_amount,cost_basis,cost_payer,cost_included_in_price. '
                                    'Use null when absent; source_location identifies searched range. Count distinct requested items.',
                                    {'document': document}, Extraction, deadline)
        fields = normalize_extraction(extraction, document, sections)
        try:
            supplier = self.references.match_supplier(fields['supplier_name'])
            item = self.references.match_item(item_name=fields['item_name'], origin=fields['origin'],
                                               grade=fields['grade'], processing=fields['processing'])
        except ValueError:
            raise ReviewRequired('REFERENCE_MATCH_FAILED', '거래처·품목을 기준자료에서 하나로 특정할 수 없습니다.')
        self._verify('request_validation', {'document': document, 'extraction': extraction.model_dump(),
                                           'normalized': fields, 'supplier_id': supplier, 'item_id': item}, deadline)
        lease.check()
        on_item(item)
        effective = fields['effective_date']
        start, end = effective.replace(day=1), effective.replace(day=calendar.monthrange(effective.year, effective.month)[1])
        contracts = {c['contract_id']: c for c in self.references.read('contracts', supplier_id=supplier, item_id=item)
                     if as_date(c['valid_from']) <= end and as_date(c['valid_to']) >= start}
        plans = []
        for contract_id, contract in contracts.items():
            plans.extend(p for p in self.references.read('purchase_plans', contract_id=contract_id)
                         if start <= as_date(p['delivery_date']) <= end)
        if not plans or len({p['plan_id'] for p in plans}) != len(plans):
            raise ReviewRequired('PLAN_UNCONFIRMED', '검토 기간의 구매계획이 없거나 중복입니다.')
        source_plan_keys = [(p['source_ref'], as_date(p['delivery_date']), p['quantity']) for p in plans]
        if len(set(source_plan_keys)) != len(source_plan_keys):
            raise ReviewRequired('DUPLICATE_PLAN', '동일 근거의 구매계획 중복 합산 가능성이 있습니다.')
        for plan in plans:
            contract = contracts[plan['contract_id']]
            if not as_date(contract['valid_from']) <= as_date(plan['delivery_date']) <= as_date(contract['valid_to']):
                raise ReviewRequired('CONTRACT_PERIOD_MISMATCH', '계획 납기가 계약 구간을 벗어났습니다.')
            if contract['unit_price'] != fields['old_unit_price']:
                raise ReviewRequired('OLD_PRICE_MISMATCH', '요청의 기존 단가와 계약 단가가 다릅니다.')
        total = sum((p['quantity'] for p in plans), ZERO)
        if fields.get('requested_quantity') is not None and fields['requested_quantity'] != total:
            raise ReviewRequired('QUANTITY_MISMATCH', '원문 수량과 검토 기간의 계획 물량이 다릅니다.')
        if fields.get('requested_delivery_date') and any(as_date(p['delivery_date']) != fields['requested_delivery_date'] for p in plans):
            raise ReviewRequired('DELIVERY_MISMATCH', '원문 납기와 구매계획을 대조해야 합니다.')
        changed = {p['plan_id'] for p in plans if contracts[p['contract_id']]['price_rule'] == 'CHANGEABLE'
                   and as_date(p['delivery_date']) >= effective}
        changeable = sum((p['quantity'] for p in plans if p['plan_id'] in changed), ZERO)
        fixed = total - changeable
        all_costs, baseline_costs = [], {'TRANSPORT': ZERO, 'OTHER_LOGISTICS': ZERO}
        for contract_no in sorted({contracts[p['contract_id']]['contract_no'] for p in plans}):
            own = [p for p in plans if contracts[p['contract_id']]['contract_no'] == contract_no]
            costs = self.references.read('cost_terms', contract_no=contract_no)
            all_costs.extend(costs)
            pair = paired_costs(costs, sum((p['quantity'] for p in own), ZERO), start, end)
            if any(value is None for value in pair.values()):
                raise ReviewRequired('COST_UNCONFIRMED', '계약 비용의 기간·물량·부담·포함 여부가 확인되지 않았습니다.')
            for key in pair:
                baseline_costs[key] += pair[key]
        after_costs = request_costs(fields, baseline_costs, total, start, end)
        references = self.references.read('price_references', item_id=item)
        candidates, excluded = [], []
        today = date.today()
        for ref in sorted(references, key=lambda r: (str(r['reference_date']), r['reference_id']), reverse=True):
            if ref['reference_type'] != 'QUOTE' or ref['supplier_id'] == supplier or changeable == 0:
                continue
            reason = None
            if unit_name(ref['unit']) != 'kg' or ref['currency'] not in ('KRW', '원'):
                reason = '단위·통화 불일치 또는 환산 근거 없음'
            elif ref['unit_price'] is None or not ref['valid_until'] or as_date(ref['reference_date']) > today or as_date(ref['valid_until']) < max(today, end):
                reason = '견적 단가·유효기간 확인 불가'
            elif ref['moq'] is not None and ref['moq'] > changeable:
                reason = '변경 가능 물량보다 MOQ가 큼'
            if reason:
                excluded.append({'reference_id': ref['reference_id'], 'reason': reason})
                continue
            candidates.append(ref)
        candidate_cost_map = {ref['reference_id']: self.references.read('cost_terms', reference_id=ref['reference_id']) for ref in candidates}
        source_refs = [p['source_ref'] for p in plans] + [contracts[p['contract_id']]['source_ref'] for p in plans]
        source_refs += [cost['source_ref'] for cost in all_costs] + [ref['source_ref'] for ref in references]
        source_refs += [cost['source_ref'] for costs in candidate_cost_map.values() for cost in costs]
        original_references = self._reference_texts(source_refs)
        supplier_rows = {row['supplier_id']: row for row in self.references.read('suppliers')}
        evidence = {'document': document, 'fields': fields, 'contracts': list(contracts.values()), 'plans': plans,
                    'references': references, 'cost_terms': all_costs, 'source_documents': original_references,
                    'candidate_cost_terms': candidate_cost_map,
                    'supplier_information': supplier_rows,
                    'candidates': candidates, 'excluded': excluded, 'period': {'from': start, 'to': end},
                    'quantities': {'total': total, 'fixed': fixed, 'changeable': changeable}}
        summary = self.agent.run('evidence_review', 'Review contract, history, quotations and retained supplier candidates together. '
                                'Separate company experience from objective price facts. Do not guarantee supply or calculate money.',
                                evidence, ReviewSummary, deadline)
        comparison = self.agent.run('comparison_validation',
                                    'Return exactly one decision for EACH reference_id in references. Compare original '
                                    'item/specification, origin/grade/processing, unit, currency, validity, MOQ, delivery and trade stage. '
                                    'Mark noncomparable references comparable=false with Korean reason; do not stop the entire request. '
                                    'Unknown optional delivery/stock/cost conditions must be listed. Do not override deterministic exclusions.',
                                    evidence, ComparisonVerdict, deadline)
        decisions = {decision.reference_id: decision for decision in comparison.decisions}
        if len(decisions) != len(comparison.decisions) or set(decisions) != {ref['reference_id'] for ref in references}:
            raise ServiceError('COMPARISON_VALIDATION_FAILED', '참고자료별 비교 판정을 확인할 수 없습니다.', 502)
        already_excluded = {entry['reference_id'] for entry in excluded}
        for decision in comparison.decisions:
            if not decision.comparable and decision.reference_id not in already_excluded:
                excluded.append({'reference_id': decision.reference_id, 'reason': decision.reason})
        selected, suppliers_seen = [], set()
        for candidate in candidates:
            if decisions[candidate['reference_id']].comparable and candidate['supplier_id'] not in suppliers_seen:
                selected.append(candidate)
                suppliers_seen.add(candidate['supplier_id'])
                if len(selected) == 2:
                    break
        candidates = selected
        evidence['candidates'] = candidates
        evidence['comparison_decisions'] = comparison.model_dump()
        self._verify('contract_validation', {**evidence, 'request_costs': after_costs}, deadline)
        lease.check()
        registry, expected, metadata = {}, {}, {}
        for option_id in ['BEFORE', 'AFTER'] + [ref['reference_id'] for ref in candidates]:
            goods = []
            ref = next((r for r in candidates if r['reference_id'] == option_id), None)
            for index, plan in enumerate(plans):
                quantity_key, price_key = f'q{index}', f'{option_id}_p{index}'
                registry[quantity_key] = plan['quantity']
                is_new = option_id != 'BEFORE' and plan['plan_id'] in changed
                registry[price_key] = (ref['unit_price'] if ref else fields['new_unit_price']) if is_new else contracts[plan['contract_id']]['unit_price']
                goods.append((price_key, quantity_key))
            costs = baseline_costs if option_id == 'BEFORE' else after_costs
            unknown = []
            if ref:
                candidate_cost_rows = candidate_cost_map[ref['reference_id']]
                costs = paired_costs(candidate_cost_rows, changeable, start, end)
                # Old monthly costs are never silently dropped when fixed/earlier deliveries remain.
                retained = {'TRANSPORT': ZERO, 'OTHER_LOGISTICS': ZERO}
                for contract_no in {contracts[p['contract_id']]['contract_no'] for p in plans}:
                    own = [p for p in plans if contracts[p['contract_id']]['contract_no'] == contract_no]
                    retained_qty = sum((p['quantity'] for p in own if p['plan_id'] not in changed), ZERO)
                    if retained_qty:
                        for cost in (c for c in all_costs if c['contract_no'] == contract_no):
                            original_qty = sum((p['quantity'] for p in own), ZERO)
                            amount = cost_value(cost, retained_qty if cost['basis'] == 'PER_UNIT' else original_qty, start, end)
                            if amount is None:
                                costs[cost['cost_type']] = None
                            else:
                                retained[cost['cost_type']] += amount
                    else:
                        for cost in (c for c in all_costs if c['contract_no'] == contract_no and c['basis'] == 'MONTHLY_FIXED'):
                            if cost_value(cost, sum((p['quantity'] for p in own), ZERO), start, end) != ZERO:
                                # No evidence of releasing the old monthly charge after full switching.
                                costs[cost['cost_type']] = None
                costs = {key: None if value is None else value + retained[key] for key, value in costs.items()}
                unknown = [key + ' 미확인' for key, value in costs.items() if value is None]
                unknown += [label + ' 미확인' for key, label in [('moq', 'MOQ'), ('lead_days', '납기 소요일'), ('delivery_terms', '배송 조건')] if ref[key] is None]
                unknown.append('재고·실제 공급 가능 여부 미확인')
                unknown.extend(decisions[ref['reference_id']].unconfirmed_conditions)
            confirmed = all(value is not None for value in costs.values())
            transport_key, other_key = option_id + '_transport', option_id + '_other'
            registry[transport_key], registry[other_key] = costs['TRANSPORT'] or ZERO, costs['OTHER_LOGISTICS'] or ZERO
            expected[option_id] = {'goods': goods, 'transport_keys': [transport_key], 'other_logistics_keys': [other_key], 'cost_confirmed': confirmed}
            metadata[option_id] = {'reference': ref, 'unknown': unknown}
        rounds, proposals = [], []
        for number in range(self.settings.calculation_rounds):
            lease.check()
            proposal = self.agent.run('calculation', 'Build a multiply-and-sum proposal using ONLY the supplied registry keys and option requirements. '
                                      'Do not return calculated monetary values. Each goods term is price key times quantity key. Include each required cost key exactly once. '
                                      'This is an independent context; no previous answer is available.',
                                      {'evidence': evidence, 'registry': registry, 'option_requirements': expected}, CalculationProposal, deadline)
            rounds.append(evaluate(proposal, registry, expected))
            proposals.append(proposal.model_dump())
        calculated = all_rounds_match(rounds, self.settings.calculation_rounds)
        self._verify('calculation_validation', {'evidence': evidence, 'registry': registry, 'proposals': proposals, 'tool_results': rounds}, deadline)
        cards = []
        for option_id in expected:
            if option_id == 'BEFORE':
                continue
            ref = metadata[option_id]['reference']
            supplier_id = ref['supplier_id'] if ref else supplier
            spec = expected[option_id]
            formula_parts = [f'{registry[quantity]:f} × {registry[price]:f}' for price, quantity in spec['goods']]
            if spec['cost_confirmed']:
                formula_parts += [format(registry[key], 'f') for key in spec['transport_keys'] + spec['other_logistics_keys']]
            cards.append({'option_id': option_id, 'type': 'CANDIDATE' if ref else 'AFTER', 'supplier_id': supplier_id,
                          'supplier_name': supplier_rows[supplier_id]['supplier_name'], 'reference_id': ref['reference_id'] if ref else None,
                          'fixed_unit_price': fields['old_unit_price'], 'changeable_unit_price': ref['unit_price'] if ref else fields['new_unit_price'],
                          **calculated[option_id], 'formula': ' + '.join(formula_parts),
                          'calculation_status': 'VERIFIED' if expected[option_id]['cost_confirmed'] else 'COST_UNCONFIRMED',
                          'unconfirmed_conditions': metadata[option_id]['unknown'], 'sources': sorted(set(source_refs))})
        texts = self.agent.run('card_composition', 'Write Korean titles and brief evidence-based explanations for exactly the supplied option IDs. '
                              'Do not add numbers, source claims, decisions or supply guarantees. Monetary fields are server-owned.',
                              {'cards': cards, 'review_summary': summary.model_dump()}, CardTexts, deadline)
        if len(texts.cards) != len(cards) or {c.option_id for c in texts.cards} != {c['option_id'] for c in cards}:
            raise ServiceError('OUTPUT_VALIDATION_FAILED', '카드 방안 구성이 일치하지 않습니다.', 502)
        for card in cards:
            text = next(text for text in texts.cards if text.option_id == card['option_id'])
            card.update(title=text.title, explanation=text.explanation)
        self._verify('output_validation', {'document': document, 'evidence': evidence, 'calculated': calculated, 'cards': cards}, deadline, output_failure=True)
        lease.check()
        if time.monotonic() > deadline:
            raise ServiceError('ANALYSIS_TIMEOUT', '분석 제한 시간을 초과했습니다.', 504)
        result = {'outcome': 'SUCCESS', 'item_id': item, 'unit': 'kg', 'currency': 'KRW', 'period': {'from': start, 'to': end},
                'quantities': {'total': total, 'fixed': fixed, 'changeable': changeable}, 'baseline': {'type': 'BEFORE', **calculated['BEFORE']},
                'cards': cards, 'extracted_fields': [field.model_dump() for field in extraction.fields],
                'candidate_search': {'status': 'FOUND' if candidates else 'NONE', 'displayed_count': len(candidates), 'excluded': excluded},
                'calculation_verification': {'rounds': self.settings.calculation_rounds, 'all_rounds_match': True, 'independent_check_passed': True},
                'issues': [], 'sources': sorted(set(source_refs)), 'result_persisted': False}
        # Validate before the service records COMPLETE, not only at the HTTP boundary.
        AnalysisResponse.model_validate(json.loads(json.dumps({'work_id': work_id, 'queue_status': 'COMPLETE', **result}, default=json_default)))
        return result
