"""시나리오 데이터 점검 (LLM 없음, 비용 없음).

각 시나리오의 세계를 만들고 결정적 도구(후보 판정·비교·역전 계산·구매안 후보·역제안 계산)만 불러,
문서의 정답(채점 키)과 도구 계산이 맞는지 본다. 여기서 어긋나면 모델을 돌리기 전에 시나리오 데이터를 고친다.
LLM이 판단할 부분(같은 품목 고르기·원문 확인·주의 표시 등)은 여기서 보지 않는다.

    python -m scripts.agent_eval.oracle            # 전체
    python -m scripts.agent_eval.oracle C-01 R-29  # 일부
"""
from __future__ import annotations

import sys
import traceback
from decimal import Decimal

from app.agents import tools as T
from app.agents.schemas import RunContext

from scripts.agent_eval.scenarios import SCENARIOS
from scripts.agent_eval.store import CASE_ID

SAME_ITEM = {'냉동 망고', 'IQF 냉동 망고 특품', '냉동 망고 (업소용)', '망고(냉동) IQF 다이스', '냉동 망고 슬라이스'}


def name(sid: str) -> str:
    return sid.removeprefix('s-')


def check_candidate(sc, world, store, ctx) -> list[str]:
    e, problems = sc.expect, []
    if world.revision_changed:
        return [] if T.load_case_context(store, ctx)['status'] == 'REVISION_CHANGED' else ['revision 변경이 안 보임']
    ids = [i['id'] for i in store.items if i['item_name'] in SAME_ITEM]
    result = T.evaluate_candidates(store, ctx, ids)
    if e.get('hold') == 'CONDITIONS_MISSING':
        return [] if result['status'] == 'CONDITIONS_MISSING' else [f'CONDITIONS_MISSING 아님: {result["status"]}']
    if result['status'] != 'OK':
        return [f'후보 판정 status {result["status"]}']
    with_contact = {name(c['supplier_id']) for c in result['candidates'] if c['has_contact']}
    if 'candidates' in e and with_contact != e['candidates']:
        problems.append(f'연락처 있는 후보 {sorted(with_contact)} ≠ 정답 {sorted(e["candidates"])}')
    if 'candidate_count' in e and len(result['candidates']) != e['candidate_count']:
        problems.append(f'후보 수 {len(result["candidates"])} ≠ {e["candidate_count"]}')
    if 'first' in e and result['candidates'] and name(result['candidates'][0]['supplier_id']) != e['first']:
        problems.append(f'첫 후보 {name(result["candidates"][0]["supplier_id"])} ≠ {e["first"]}')
    for supplier, score in (e.get('score') or {}).items():
        got = next((c['preference_score'] for c in result['candidates'] if name(c['supplier_id']) == supplier), None)
        if got != score:
            problems.append(f'{supplier} 점수 {got} ≠ {score}')
    return problems


def check_priority(sc, world, store, ctx) -> list[str]:
    e, problems = sc.expect, []
    if world.revision_changed:
        return [] if T.load_case_context(store, ctx)['status'] == 'REVISION_CHANGED' else ['revision 변경이 안 보임']
    result = T.compare_quotes(store, ctx)
    if e['action'] == 'HOLD':
        return [] if result['status'] != 'OK' else ['비교가 가능함(HOLD 정답과 다름)']
    rows = {name(s['supplier_id']): s for s in result['suppliers']}
    open_fields = {(n, f) for n, s in rows.items() for f in s['missing_required']} | \
                  {(n, c['field']) for n, s in rows.items() for c in s['unknown_costs']} | \
                  {(n, r['field']) for n, s in rows.items() for r in s['requirements'] if r['state'] == 'CHECK'}
    for supplier, field_, priority in e.get('questions') or set():
        if (supplier, field_) not in open_fields:
            problems.append(f'질문 정답 {supplier}.{field_}가 도구 미확인 목록에 없음')
        if priority == 'CAN':
            be = T.break_even_for(store, ctx, f's-{supplier}')
            if not be.get('can_change_choice'):
                problems.append(f'{supplier}.{field_} CAN인데 역전 계산 can_change_choice 아님')
    for supplier, field_ in e.get('low') or set():
        be = T.break_even_for(store, ctx, f's-{supplier}')
        if not be.get('proven_low_value'):
            problems.append(f'{supplier}.{field_} LOW인데 proven_low_value 아님')
    for supplier, field_, text in e.get('recheck') or []:
        if (supplier, field_) not in open_fields:
            problems.append(f'재추출 정답 {supplier}.{field_}가 도구 미확인 목록에 없음')
        if text not in store.mails[f'm-{supplier}']['body'] and not any(
                text in page for a in store.mails[f'm-{supplier}']['attachments'] for page in a['texts']):
            problems.append(f'재추출 구절 "{text}"가 원문에 없음')
    if e['action'] == 'BUILD_PLANS':
        unexpected = {(n, f) for n, f in open_fields} - {(s, f) for s, f in e.get('low', set())}
        # 충족 판단이 필요한 조건(CHECK)은 LLM이 원문으로 판단한다. 필수 누락만 본다
        hard = {(n, f) for n, s in rows.items() for f in s['missing_required']} - set(e.get('low', set()))
        if hard and sc.id not in ('P-16',):
            problems.append(f'BUILD_PLANS인데 필수 누락 {sorted(hard)}')
        if unexpected and not (set(unexpected) <= {(n, r['field']) for n, s in rows.items() for r in s['requirements']}
                               | {(n, c['field']) for n, s in rows.items() for c in s['unknown_costs']}):
            problems.append(f'미확인 {sorted(unexpected)}')
    return problems


def _plan_key(plan) -> tuple:
    return plan['plan_type'], {name(a['supplier_id']): int(Decimal(a['quantity'])) for a in plan['allocations']}


def check_plan(sc, world, store, ctx) -> list[str]:
    e, problems = sc.expect, []
    if world.revision_changed:
        return [] if T.load_case_context(store, ctx)['status'] == 'REVISION_CHANGED' else ['revision 변경이 안 보임']
    if e['action'] == 'RECHECK_SOURCE':
        rows = {name(s['supplier_id']): s for s in T.compare_quotes(store, ctx)['suppliers']}
        for supplier, field_, text in e['recheck']:
            s = rows[supplier]
            if field_ not in s['missing_required'] and field_ not in [c['field'] for c in s['unknown_costs']]:
                problems.append(f'{supplier}.{field_} 도구 미확인 아님')
            if text not in store.mails[f'm-{supplier}']['body']:
                problems.append(f'구절 "{text}" 원문 없음')
        return problems
    met = [T.MetRequirement(supplier_id=f's-{s}', requirement=r) for s, r in e.get('oracle_met', set())]
    result = T.plan_candidates(store, ctx, met)
    if result.get('status') != 'OK':
        return [] if e['action'] in ('HOLD',) or isinstance(e['action'], set) else [f'구매안 후보 status {result.get("status")}']
    got = [p for p in result['plans'] if p['status'] in ('VALID', 'CONDITIONAL')]
    if e['action'] == 'HOLD' and got:
        problems.append(f'HOLD 정답인데 가능한 안 {len(got)}개')
    for plan_type, allocation, total, *status in e.get('plans') or []:
        match = [p for p in got if _plan_key(p) == (plan_type, allocation)]
        if not match:
            problems.append(f'필수 안 없음 {plan_type} {allocation}')
            continue
        p = match[0]
        if int(Decimal(p['total_amount'])) != total:
            problems.append(f'{plan_type} {allocation} 총액 {p["total_amount"]} ≠ {total}')
        if p['status'] != (status or ['VALID'])[0]:
            problems.append(f'{plan_type} {allocation} 상태 {p["status"]} ≠ {(status or ["VALID"])[0]}')
    return problems


def check_counter(sc, world, store, ctx, inputs) -> list[str]:
    e, problems = sc.expect, []
    if world.revision_changed:
        return [] if T.load_case_context(store, ctx)['status'] == 'REVISION_CHANGED' else ['revision 변경이 안 보임']
    plans = inputs.get('validated_plans', [])
    candidates = [p for p in plans if p.plan_type != 'KEEP' and p.validation_status == 'VALID']
    if not candidates:
        return [] if e['action'] == 'HOLD' else ['VALID 대안 없음']
    best = min(candidates, key=lambda p: Decimal(p.total_amount))
    if 'alternative' in e and {name(a.supplier_id): int(Decimal(a.quantity)) for a in best.allocations} != e['alternative']:
        problems.append(f'최저 대안 {[(a.supplier_id, a.quantity) for a in best.allocations]} ≠ {e["alternative"]}')
    alternative = [T.AlternativeAllocation(supplier_id=a.supplier_id, quote_version_id=a.quote_version_id, quantity=a.quantity)
                   for a in best.allocations]
    numbers = T.counter_offer_numbers(store, ctx, alternative)
    if numbers.get('status') != 'OK':
        return [] if e['action'] == 'HOLD' else [f'역제안 계산 {numbers}']
    status = numbers['counter_status']
    if e.get('ref_status') and status != e['ref_status']:
        problems.append(f'계산 상태 {status} ≠ {e["ref_status"]}')
    if e.get('target') and numbers.get('target_unit_price') != e['target']:
        problems.append(f'목표 {numbers.get("target_unit_price")} ≠ {e["target"]}')
    if e.get('range') and tuple(numbers.get('allowed_range') or ()) != e['range']:
        problems.append(f'범위 {numbers.get("allowed_range")} ≠ {e["range"]}')
    for word in e.get('reason_has') or []:
        if word == '비싸' and not numbers.get('above_alternative'):
            problems.append('대안보다 비쌈 정답인데 above_alternative false')
    for word in e.get('reason_not') or []:
        if word == '비싸' and numbers.get('above_alternative'):
            problems.append('대안보다 비싸지 않음 정답인데 above_alternative true')
    return problems


def main(ids: list[str]):
    bad = 0
    for sc in SCENARIOS:
        if ids and sc.id not in ids:
            continue
        try:
            world = sc.world()
            store = world.build()
            inputs = sc.inputs(world, store) if sc.inputs else {}
            revision = world.agent_revision(store)
            ctx = RunContext(CASE_ID, revision)
            check = {'CANDIDATE': check_candidate, 'INFO_PRIORITY': check_priority, 'RESPONSE_PLAN': check_plan}.get(sc.role)
            problems = check(sc, world, store, ctx) if check else check_counter(sc, world, store, ctx, inputs)
        except Exception as error:
            problems = [f'예외 {type(error).__name__}: {error}']
            traceback.print_exc()
        if problems:
            bad += 1
            print(f'✗ {sc.id} {sc.title}')
            for p in problems:
                print(f'    - {p}')
    print(f'\n점검 {len([s for s in SCENARIOS if not ids or s.id in ids])}개 중 어긋남 {bad}개')


if __name__ == '__main__':
    main(sys.argv[1:])
