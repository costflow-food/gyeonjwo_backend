"""run.py 결과(JSONL) 채점. 다시 돌리지 않고 몇 번이든 다시 채점할 수 있다.

    python -m scripts.agent_eval.grade eval_results/<실행 폴더>

판정
- PASS / FAIL: 시나리오 채점 키와 문서의 공통 실패 조건으로 자동 판정
- EXCLUDED: 문서의 채점 제외 행 (분모에서 뺀다)
- RUN_ERROR: API 키·속도 제한·네트워크처럼 에이전트와 무관한 실행 오류 (분모에서 빼고 다시 돌린다)
에이전트 쪽 예외(도구 호출 한도 초과, 최종 응답 형식 오류 등)는 FAIL이다.
👁(사람 확인) 항목은 자동 판정에 넣지 않고 결과표의 human 칸에 적는다.

expect 키 (scenarios.py)
  공통: action(문자열 또는 집합) · hold(코드 또는 None) · drafts(저장된 초안 수) · only_tools(도구 호출 순서 전체)
  ②: candidates(업체 집합) · candidate_count · caution{업체: bool} · reason_has/reason_not{업체: [문구]} · first · score
      · questions{업체: {has, not}}(초안 본문의 선택 질문) · no_due_question · evidence_not/evidence_has_if_searched/
      evidence_items_all(근거 조회 품목) · draft_items_has(초안 도구 품목) · search_keyword_has · search_max · web_calls
      · draft_calls · reason_overall_has
  ③: questions{(업체, 필드, MUST|CAN)}(정확히 일치) · low · not_low · not_ask · recheck[(업체, 필드, 원문 구절)]
      · recheck_not · no_recheck · questions_must_can
  ④: plans[(유형, {업체: 배정량}, 총액[, 상태])] · forbid · met_has/met_not · read_mails · no_plans · no_recheck · recheck
  ⑤: target · range · alternative · ref_status · reason_has/reason_not · conditions_not · unknown_has · recheck · no_recheck
      · draft_calls_max
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from scripts.agent_eval.scenarios import BY_ID, ROLE_CODES

OPTION_TEXT = {'MOQ': '최소 주문 수량(MOQ)', 'VALIDITY': '견적 유효기간', 'TAX': '부가세 포함 여부', 'ORIGIN': '원산지',
               'DELIVERY_DAYS': '배송 가능 요일과 지역'}
DUE_Q, NO_DUE_Q = '요청 납기일 납품 가능 여부', '납품 가능 일정(발주 후 납품까지 걸리는 기간)'
PRIORITY = {'MUST_CONFIRM': 'MUST', 'CAN_CHANGE_CHOICE': 'CAN', 'LOW_VALUE': 'LOW'}
ENGLISH_CODE = re.compile(r'\b[A-Z]{2,}(?:_[A-Z]+)+\b|supplier_item_id|quote_version_id')
# "대안보다 비싸다"는 사실을 적었는지: 활용형(비쌉니다)·같은 뜻(비용이 높습니다·초과) 포함
PRICIER = re.compile(r'비싸|비쌉|비쌀|비싼|높습|높아|높은|높다|높게|높을|높으|초과|웃돌|상회')
AMOUNT = re.compile(r'\d{1,3}(?:,\d{3})+\s*원|\d{4,}\s*원')
RUN_ERRORS = ('AuthenticationError', 'RateLimitError', 'APIConnectionError', 'APITimeoutError', 'PermissionDeniedError',
              'InternalServerError', 'NotFoundError', 'BadRequestError', 'KeyError: \'EVAL_OPENAI_API_KEY\'',
              'OpenAIInvalidRequestError', 'OpenAIAuthenticationError', 'OpenAIRateLimitError', 'OpenAIModelNotFoundError',
              'OpenAIPermissionDeniedError', 'OpenAIAPIError', 'OpenAIConnectionError', 'OpenAITimeoutError')


def squash(text) -> str:
    return re.sub(r'\s+', '', str(text or ''))


class Check:
    def __init__(self, record: dict):
        self.r, self.fails, self.search_miss = record, [], False
        self.out = record.get('output') or {}
        names = record.get('names') or {}
        self.name = lambda sid: names.get(sid, str(sid).removeprefix('s-'))

    def fail(self, text: str):
        self.fails.append(text)

    def tools(self, name: str) -> list[dict]:
        return [t for t in self.r.get('tools') or [] if t['tool'] == name]


def common(c: Check, e: dict):
    out = c.out
    action = e.get('action')
    if action is not None:
        allowed = action if isinstance(action, set) else {action}
        if out.get('next_action') not in allowed:
            c.fail(f"next_action {out.get('next_action')} ≠ {'/'.join(sorted(allowed))}")
    if 'hold' in e:
        got = (out.get('hold') or {}).get('code')
        if got != e['hold']:
            c.fail(f'hold {got} ≠ {e["hold"]}')
    if 'drafts' in e and len(c.r.get('drafts') or []) != e['drafts']:
        c.fail(f"저장된 초안 {len(c.r.get('drafts') or [])}개 ≠ {e['drafts']}")
    if 'only_tools' in e:
        called = [t['tool'] for t in c.r.get('tools') or []]
        if called != e['only_tools']:
            c.fail(f'도구 호출 {called} ≠ {e["only_tools"]}')


def candidate(c: Check, e: dict):
    out = c.out
    rows = {c.name(row['supplier_id']): row for row in out.get('candidates') or []}
    if 'candidates' in e and set(rows) != e['candidates']:
        c.fail(f'후보 {sorted(rows)} ≠ {sorted(e["candidates"])}')
    if 'candidate_count' in e and len(rows) != e['candidate_count']:
        c.fail(f'후보 수 {len(rows)} ≠ {e["candidate_count"]}')
    for supplier, flag in (e.get('caution') or {}).items():
        if supplier in rows and bool(rows[supplier].get('caution')) != flag:
            c.fail(f'{supplier} 주의 표시 {"있음" if not flag else "없음"}(정답 {"있음" if flag else "없음"})')
    for supplier, words in (e.get('reason_has') or {}).items():
        reason = squash((rows.get(supplier) or {}).get('reason'))
        missing = [w for w in words if squash(w) not in reason]
        if supplier not in rows or missing:
            c.fail(f'{supplier} 사유에 {missing or words} 없음')
    for supplier, words in (e.get('reason_not') or {}).items():
        reason = squash((rows.get(supplier) or {}).get('reason')) + squash(((rows.get(supplier) or {}).get('caution') or {}).get('reason'))
        found = [w for w in words if squash(w) in reason]
        if found:
            c.fail(f'{supplier} 사유에 {found}')
    if 'first' in e:
        order = [c.name(row['supplier_id']) for row in out.get('candidates') or []]
        if not order or order[0] != e['first']:
            c.fail(f'첫 후보 {order[:1]} ≠ {e["first"]}')
    for supplier, score in (e.get('score') or {}).items():
        got = (rows.get(supplier) or {}).get('preference_score')
        if got is None or Decimal(got) != Decimal(score):
            c.fail(f'{supplier} 점수 {got} ≠ {score}')
    # 공통: 사유 형식(명사형, 마침표 없음)·영문 코드
    for supplier, row in rows.items():
        reason = (row.get('reason') or '').strip()
        if reason.endswith('.') or reason.endswith('입니다'):
            c.fail(f'{supplier} 사유가 문장형("{reason[-12:]}")')
        if ENGLISH_CODE.search(reason):
            c.fail(f'{supplier} 사유에 영문 코드')
    if ENGLISH_CODE.search(out.get('reason') or ''):
        c.fail('전체 사유에 영문 코드')
    # 초안 본문의 질문
    bodies = {c.name(d['supplier_id']): d['body'] for d in c.r.get('drafts') or []}
    wanted = e.get('questions') or {}
    for supplier, body in bodies.items():
        spec = wanted.get(supplier, {})
        required = (set(spec.get('has', [])) | {'MOQ', 'VALIDITY'}) - set(spec.get('not', []))
        for key in sorted(required):
            if OPTION_TEXT[key] not in body:
                c.fail(f'{supplier} 초안에 "{OPTION_TEXT[key]}" 질문 없음')
        for key in spec.get('not', []):
            if OPTION_TEXT[key] in body:
                c.fail(f'{supplier} 초안에 이미 답한 "{OPTION_TEXT[key]}" 질문')
    for supplier in set(wanted) - set(bodies):
        if supplier in (e.get('candidates') or set()):
            c.fail(f'{supplier} 초안 없음')
    if e.get('no_due_question'):
        for supplier, body in bodies.items():
            if NO_DUE_Q not in body or DUE_Q in body:
                c.fail(f'{supplier} 초안의 납기 질문이 "납품 가능 일정"이 아님')
    evidence_args = [set(t['args'].get('supplier_item_ids') or []) for t in c.tools('search_supplier_evidence')]
    draft_args = [set(t['args'].get('supplier_item_ids') or []) for t in c.tools('create_rfq_draft')]
    sent = set().union(*evidence_args) if evidence_args else set()
    for item in e.get('evidence_not') or set():
        if item in sent:
            c.fail(f'다른 품목 {item}을 근거 조회에 보냄')
    found = {row['supplier_item_id'] for t in c.tools('search_supplier_items') for row in (t.get('result') or {}).get('items') or []}
    for item in e.get('evidence_has_if_searched') or set():
        if item not in found:
            c.search_miss = True      # 문서: 검색에서 행을 찾지 못하면 품목 판정과 구분해 따로 기록한다
        elif item not in sent:
            c.fail(f'검색된 동등 품목 {item}을 근거 조회에서 뺌')
    for item in e.get('evidence_items_all') or set():
        if not evidence_args or any(item not in args for args in evidence_args):
            c.fail(f'근거 조회 품목에 {item} 없음')
        if not draft_args or any(item not in args for args in draft_args):
            c.fail(f'초안 도구 품목에 {item} 없음')
    for item in e.get('draft_items_has') or set():
        if not draft_args or any(item not in args for args in draft_args):
            c.fail(f'초안 도구 품목에 {item} 없음')
    searches = c.tools('search_supplier_items')
    if 'search_keyword_has' in e and not any(e['search_keyword_has'] in ' '.join(t['args'].get('keywords') or [])
                                             for t in searches):
        c.fail(f'검색어에 "{e["search_keyword_has"]}" 없음')
    if 'search_max' in e and len(searches) > e['search_max']:
        c.fail(f'검색 {len(searches)}회 > {e["search_max"]}')
    keywords = [tuple(sorted(squash(k) for k in t['args'].get('keywords') or [])) for t in searches]
    if len(keywords) != len(set(keywords)):
        c.fail('띄어쓰기만 다른(또는 같은) 검색어 중복 전송')
    web = c.tools('crawl_supplier_candidates')
    if 'web_calls' in e and len(web) != e['web_calls']:
        c.fail(f'웹 검색 {len(web)}회 ≠ {e["web_calls"]}')
    rfq_calls = c.tools('create_rfq_draft')
    if len(rfq_calls) > e.get('draft_calls', 1):
        c.fail(f'초안 도구 {len(rfq_calls)}회 호출')
    for word in e.get('reason_overall_has') or []:
        if squash(word) not in squash(out.get('reason')):
            c.fail(f'전체 사유에 "{word}" 없음')


def _questions(c: Check, kinds=('MUST', 'CAN')) -> set:
    return {(c.name(q['supplier_id']), q['field'], PRIORITY.get(q['priority'], q['priority']))
            for q in c.out.get('questions') or [] if PRIORITY.get(q['priority']) in kinds}


def _recheck(c: Check, e: dict):
    rows = c.out.get('recheck') or []
    for supplier, field_, text in e.get('recheck') or []:
        match = [r for r in rows if c.name(r['supplier_id']) == supplier and r['field'] == field_]
        if not match:
            c.fail(f'재추출 {supplier}.{field_} 없음')
        elif not any(squash(text) in squash(r['source_text']) for r in match):
            c.fail(f'재추출 인용 "{match[0]["source_text"]}"에 원문 구절 "{text}" 없음')
    for supplier, field_ in e.get('recheck_not') or set():
        if any(c.name(r['supplier_id']) == supplier and r['field'] == field_ for r in rows):
            c.fail(f'재추출하지 않아야 할 {supplier}.{field_}를 넣음')
    if e.get('no_recheck') and rows:
        c.fail(f'재추출 {len(rows)}건(없어야 함)')


def priority(c: Check, e: dict):
    asked = _questions(c)
    if 'questions' in e and asked != e['questions']:
        c.fail(f'질문 {sorted(asked)} ≠ {sorted(e["questions"])}')
    if 'questions_must_can' in e and asked != e['questions_must_can']:
        c.fail(f'질문 {sorted(asked)}(없어야 함)')
    low = {(s, f) for s, f, _ in _questions(c, ('LOW',))}
    for item in e.get('low') or set():
        if item not in low:
            c.fail(f'{item[0]}.{item[1]} LOW 기록 없음')
    for item in e.get('not_low') or set():
        if item in low:
            c.fail(f'{item[0]}.{item[1]}를 LOW로 기록(미확인으로 남겨야 함)')
    for item in e.get('not_ask') or set():
        if any((s, f) == item for s, f, _ in asked):
            c.fail(f'이미 확인된 {item[0]}.{item[1]}를 다시 물음')
    _recheck(c, e)


def _plan_key(c: Check, plan: dict) -> tuple:
    return plan['plan_type'], {c.name(a['supplier_id']): int(Decimal(a['quantity'])) for a in plan['allocations']}


def plan(c: Check, e: dict):
    plans = c.out.get('plans') or []
    keyed = [(_plan_key(c, p), p) for p in plans]
    for plan_type, allocation, total, *status in e.get('plans') or []:
        match = [p for key, p in keyed if key == (plan_type, allocation)]
        if not match:
            c.fail(f'필수 안 없음: {plan_type} {allocation}')
            continue
        p = match[0]
        if p.get('total_amount') is None or int(Decimal(p['total_amount'])) != total:
            c.fail(f'{plan_type} {allocation} 총액 {p.get("total_amount")} ≠ {total}')
        if p.get('validation_status', 'VALID') != (status or ['VALID'])[0]:
            c.fail(f'{plan_type} {allocation} 상태 {p.get("validation_status")} ≠ {(status or ["VALID"])[0]}')
    for kind, target in e.get('forbid') or []:
        for (plan_type, allocation), p in keyed:
            suppliers = set(allocation)
            hit = (kind == 'ANY' and target & suppliers) or \
                  (kind == 'SPLIT_EXACT' and plan_type == 'SPLIT' and allocation == target) or \
                  (kind == plan_type and (target is None or suppliers == set(target)))
            if hit:
                c.fail(f'없어야 할 안: {plan_type} {allocation}')
    allowed_conditional = any((s or [''])[0] == 'CONDITIONAL' for *_, s in [(r[0], r[1], r[2], list(r[3:])) for r in e.get('plans') or []])
    for (plan_type, allocation), p in keyed:
        if p.get('validation_status') == 'CONDITIONAL' and not (allowed_conditional and '해담과일' in allocation):
            c.fail(f'미확인 조건이 없는 안을 조건부로 표시: {plan_type} {allocation}')
        reason = p.get('reason') or ''
        if any(word in reason for word in ('최적', '추천')) or AMOUNT.search(reason) or ENGLISH_CODE.search(reason):
            c.fail(f'안 사유에 금지 표현·금액·영문 코드: "{reason[:60]}"')
        if p.get('validation_status', 'VALID') == 'VALID' and any(w in reason for w in ('조건부', '확인 필요')):
            c.fail(f'상태는 VALID인데 사유가 조건부: {plan_type} {allocation}')
    overall = c.out.get('reason') or ''
    if any(word in overall for word in ('최적', '추천')) or ENGLISH_CODE.search(overall):
        c.fail('전체 사유에 "최적"·"추천"·영문 코드')
    met_args = [m for name in ('build_plan_candidates', 'validate_purchase_plan') for t in c.tools(name)
                for m in t['args'].get('met_requirements') or []]
    met = {(c.name(m['supplier_id']), m['requirement']) for m in met_args}
    for item in e.get('met_has') or set():
        if item not in met:
            c.fail(f'충족 판단 met_requirements에 {item} 없음')
    for item in e.get('met_not') or set():
        if item in met:
            c.fail(f'미충족 {item}을 충족으로 넣음')
    if 'read_mails' in e:
        read = {c.name('s-' + t['args'].get('mail_id', '').removeprefix('m-')) for t in c.tools('read_mail_evidence')}
        extra = read - e['read_mails']
        if extra:     # 문서 실패 조건: 판단할 필요가 없는 메일까지 읽음 (필요한 메일을 안 읽은 것은 도구 결과로 판단했으면 실패 아님)
            c.fail(f'판단할 필요 없는 메일까지 읽음: {sorted(extra)}')
    if e.get('no_plans') and plans:
        c.fail(f'구매안 {len(plans)}개(없어야 함)')
    _recheck(c, e)


def counter(c: Check, e: dict):
    out = c.out
    target = out.get('target') or {}
    reference = out.get('reference') or {}
    if 'target' in e:
        got = target.get('target_unit_price')
        if (e['target'] is None) != (got is None) or (got is not None and Decimal(got) != Decimal(e['target'])):
            c.fail(f'목표 단가 {got} ≠ {e["target"]}')
    if 'range' in e and tuple(reference.get('allowed_range') or ()) != e['range']:
        c.fail(f'허용 범위 {reference.get("allowed_range")} ≠ {list(e["range"])}')
    if 'alternative' in e:
        got = {c.name(a['supplier_id']): int(Decimal(a['quantity'])) for a in target.get('alternative') or []}
        if got != e['alternative']:
            c.fail(f'비교 대안 {got} ≠ {e["alternative"]}')
    if 'ref_status' in e and reference.get('status') != e['ref_status']:
        c.fail(f'참고 계산 상태 {reference.get("status")} ≠ {e["ref_status"]}')
    reason = squash(out.get('reason'))
    if e.get('reason_has') and not PRICIER.search(reason):
        c.fail('사유에 대안보다 비싸다는 사실 없음')
    if e.get('reason_not') and (m := PRICIER.search(reason)):
        c.fail(f'대안보다 비싸지 않은데 사유에 "{m.group(0)}" 표현')
    texts = squash(' '.join(target.get('conditions') or [])) + ''.join(squash(d['body']) for d in c.r.get('drafts') or [])
    for word in e.get('conditions_not') or []:
        if squash(word) in texts:
            c.fail(f'역제안 조건·초안에 다른 업체 정보 "{word}"')
    if 'unknown_has' in e and not any(e['unknown_has'] in field for field in out.get('unknown_fields') or []):
        c.fail(f'unknown_fields에 {e["unknown_has"]} 없음')
    calls = c.tools('create_counter_offer_draft')
    if len(calls) > e.get('draft_calls_max', 1):
        c.fail(f'역제안 초안 도구 {len(calls)}회 호출')
    if len(c.r.get('drafts') or []) > 1:
        c.fail('메일 초안 2개 이상')
    raw = out.get('reason') or ''
    if AMOUNT.search(raw) or ENGLISH_CODE.search(raw):
        c.fail('사유에 금액·영문 코드')
    if re.search(r'정책\s*(이|가)?\s*(없|미설정|미정)', raw):
        c.fail('"회사 정책 없음"을 근거로 씀')
    _recheck(c, e)


GRADERS = {'CANDIDATE': candidate, 'INFO_PRIORITY': priority, 'RESPONSE_PLAN': plan, 'COUNTER_OFFER': counter}


def grade_record(record: dict) -> dict:
    sc = BY_ID[record['id']]
    result = {'id': sc.id, 'repeat': record.get('repeat'), 'human': sc.human or ''}
    if record['status'] == 'ERROR':
        error = record.get('error') or ''
        if any(error.startswith(name) or name in error for name in RUN_ERRORS):
            return result | {'verdict': 'RUN_ERROR', 'failures': [error[:300]]}
        return result | {'verdict': 'EXCLUDED' if sc.excluded else 'FAIL', 'failures': [f'에이전트 실행 실패: {error[:300]}']}
    if record['status'] != 'OK':
        return result | {'verdict': 'RUN_ERROR', 'failures': [record['status']]}
    c = Check(record)
    common(c, sc.expect)
    GRADERS[sc.role](c, sc.expect)
    if c.search_miss and not sc.excluded:
        return result | {'verdict': 'SEARCH_MISS', 'failures': ['검색어로 해당 품목을 찾지 못함(검색 누락, 품목 판정과 구분)'] + c.fails,
                         'excluded': ''}
    verdict = 'EXCLUDED' if sc.excluded else ('PASS' if not c.fails else 'FAIL')
    return result | {'verdict': verdict, 'failures': c.fails, 'excluded': sc.excluded or ''}


def main(folder: str):
    folder = Path(folder)
    records = [json.loads(line) for line in (folder / 'runs.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    graded = [(r, grade_record(r)) for r in records]
    meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8')) if (folder / 'meta.json').exists() else {}

    with open(folder / 'results.csv', 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['역할', '시나리오', '반복', '상황', '판정', '실패 사유', '사람 확인(👁)', '채점 제외 사유', 'next_action',
                         '응답 시간(초)', '모델 호출', '입력 토큰', '캐시 입력', '출력 토큰', '추정 비용(USD)'])
        for r, g in sorted(graded, key=lambda x: (x[0]['id'][0], x[0]['id'], x[0]['repeat'])):
            u = r.get('usage') or {}
            writer.writerow([ROLE_CODES[r['role']], r['id'], r['repeat'], r['title'], g['verdict'], ' / '.join(g['failures']),
                             g['human'], g.get('excluded', ''), (r.get('output') or {}).get('next_action', ''), r.get('seconds'),
                             u.get('calls'), u.get('input'), u.get('cached'), u.get('output'), u.get('usd')])

    by_role = defaultdict(Counter)
    by_scenario = defaultdict(Counter)
    cost, seconds = Decimal(0), []
    for r, g in graded:
        by_role[r['role']][g['verdict']] += 1
        by_scenario[r['id']][g['verdict']] += 1
        cost += Decimal((r.get('usage') or {}).get('usd') or 0)
        seconds.append(r.get('seconds') or 0)
    lines = [f'# 에이전트 평가 결과 ({folder.name})', '',
             f'- 커밋 `{meta.get("commit")}` · 모델 설정 {json.dumps(meta.get("options", {}), ensure_ascii=False)}',
             f'- 프롬프트 버전 {json.dumps(meta.get("prompt_versions", {}), ensure_ascii=False)}',
             f'- 실행 {len(records)}건 · 추정 비용 ${cost:.2f} · 응답 시간 중앙값 {sorted(seconds)[len(seconds) // 2] if seconds else 0}초',
             '- 통과율 = PASS ÷ (PASS + FAIL). EXCLUDED(문서 채점 제외)·SEARCH_MISS(검색 누락, 문서대로 따로 기록)·RUN_ERROR(API 오류)는 분모에서 뺀다.', '',
             '| 역할 | PASS | FAIL | 통과율 | 제외 | 검색 누락 | 실행 오류 |', '| --- | --- | --- | --- | --- | --- | --- |']
    total = Counter()
    for role in ('CANDIDATE', 'INFO_PRIORITY', 'RESPONSE_PLAN', 'COUNTER_OFFER'):
        k = by_role.get(role)
        if not k:
            continue
        total += k
        denom = k['PASS'] + k['FAIL']
        lines.append(f'| {ROLE_CODES[role]} {role} | {k["PASS"]} | {k["FAIL"]} | {k["PASS"] / denom:.1%} | {k["EXCLUDED"]} | {k["SEARCH_MISS"]} | {k["RUN_ERROR"]} |'
                     if denom else f'| {ROLE_CODES[role]} {role} | 0 | 0 | - | {k["EXCLUDED"]} | {k["SEARCH_MISS"]} | {k["RUN_ERROR"]} |')
    denom = total['PASS'] + total['FAIL']
    if denom:
        lines.append(f'| 전체 | {total["PASS"]} | {total["FAIL"]} | {total["PASS"] / denom:.1%} | {total["EXCLUDED"]} | {total["SEARCH_MISS"]} | {total["RUN_ERROR"]} |')
    repeats = max((r.get('repeat') or 1 for r in records), default=1)
    if repeats > 1:
        unstable = [sid for sid, k in by_scenario.items() if k['PASS'] and k['FAIL']]
        lines += ['', f'## 반복 간 결과가 갈린 시나리오 ({len(unstable)}개)', '',
                  ', '.join(f'{sid}({by_scenario[sid]["PASS"]}/{by_scenario[sid]["PASS"] + by_scenario[sid]["FAIL"]})'
                            for sid in sorted(unstable)) or '없음']
    # 검증자 개입: 지적(REVISE·근거가 확인된 지적 있음)이 한 번이라도 있었던 실행과 그 최종 판정
    logged = [(r, g) for r, g in graded if 'verifier' in r]
    if logged:
        touched = [(r, g) for r, g in logged if any(v.get('issues') for v in r['verifier'])]
        calls = sum(len(r['verifier']) for r, _ in logged)
        lines += ['', '## LLM 검증자 개입', '',
                  f'- 검증자 호출 {calls}회 (실행 {len(logged)}건). 지적이 있었던 실행 {len(touched)}건: '
                  f'최종 PASS {sum(g["verdict"] == "PASS" for _, g in touched)} · FAIL {sum(g["verdict"] == "FAIL" for _, g in touched)}',
                  '- 지적이 있었던 실행 중 FAIL은 검증이 막았거나 고치지 못한 경우다. 켬·끔 비교는 compare.py로 본다.']
        codes = Counter(i.get('code') for r, _ in touched for v in r['verifier'] for i in v.get('issues') or [])
        if codes:
            lines.append('- 지적 코드: ' + ', '.join(f'{code} {n}회' for code, n in codes.most_common()))
    lines += ['', '## 실패 사유', '']
    for r, g in sorted(graded, key=lambda x: (x[0]['id'][0], x[0]['id'], x[0]['repeat'])):
        if g['verdict'] in ('FAIL', 'RUN_ERROR', 'SEARCH_MISS'):
            lines.append(f'- **{r["id"]}#{r["repeat"]}** {r["title"]} — {g["verdict"]}: ' + ' / '.join(g['failures']))
            for gate in r.get('draft_gate') or []:
                notes = '; '.join(str(i.get('explanation') or i.get('code') or i)[:160] for i in gate.get('issues') or [])
                lines.append(f'    - 초안 검증 {gate.get("state")} {gate.get("code", "")}: {notes}')
    (folder / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))
    print(f'\n→ {folder / "summary.md"} · {folder / "results.csv"}')


if __name__ == '__main__':
    if len(sys.argv) > 1:
        main(sys.argv[1])
    else:
        root = Path(__file__).resolve().parents[2] / 'eval_results'
        latest = sorted(p for p in root.iterdir() if p.is_dir() and (p / 'runs.jsonl').exists())[-1]
        print(f'가장 최근 결과를 채점합니다: {latest.name}')
        main(str(latest))
