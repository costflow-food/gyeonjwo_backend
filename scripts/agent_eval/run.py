"""②~⑤ 에이전트 평가 실행기 (B 방식: 실제 에이전트 코드·도구·LLM 검증자, DB 대신 메모리 저장소).

- 시나리오마다 새 프로세스에서 새 저장소·새 대화로 실행한다. 앞 시나리오의 문맥·저장 값이 남지 않는다.
- API 키는 서버 .env를 읽지 않고 EVAL_OPENAI_API_KEY 환경변수에서만 읽는다(평가 비용을 서버 키와 나누어 관리).
- 결과는 실행마다 JSONL 한 줄(시나리오·반복 번호별): 최종 응답, 저장된 초안, 도구 호출 기록, 토큰·추정 비용, 시간.
  채점은 grade.py가 이 파일을 읽어 따로 한다(다시 돌리지 않고 다시 채점할 수 있다).

사용 예 (저장소 루트에서):
    $env:EVAL_OPENAI_API_KEY = "sk-..."                  # PowerShell
    python -m scripts.agent_eval.run --only C-01 P-01 --dry-plan      # 무엇을 돌릴지만 보기 (호출 없음)
    python -m scripts.agent_eval.run --only C-01 P-01 R-01 O-01       # 4건 시험 실행
    python -m scripts.agent_eval.run --model gpt-6.1-terra --repeat 3 --workers 6
    python -m scripts.agent_eval.grade eval_results/<실행 폴더>
"""
from __future__ import annotations

import argparse
import functools
import json
import multiprocessing
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# 이 실행기는 LangSmith에 기록하지 않는다(원하면 --langsmith)
for key in ('LANGSMITH_TRACING', 'LANGCHAIN_TRACING_V2'):
    os.environ.setdefault(key, 'false')

ROOT = Path(__file__).resolve().parents[2]

# 1M 토큰당 USD (입력, 캐시 입력, 출력). 팀 CSV(Codex 대화 평가)의 환산 비용에서 역산한 추정값이다.
# 실제 청구 단가는 OpenAI 가격표로 확인한다. 모르는 모델은 비용을 비워 둔다
PRICES = {'gpt-6.1-terra': (1.0, 0.05, 4.0), 'gpt-6-luna': (0.10, 0.01, 0.50), 'gpt-6.2-astra': (8.0, 0.40, 40.0),
          'gpt-6.2-sol': (2.15, 0.14, 4.55)}

ROLE_IN = {'CANDIDATE': 'CandidateIn', 'INFO_PRIORITY': 'PriorityIn', 'RESPONSE_PLAN': 'PlanIn', 'COUNTER_OFFER': 'CounterOfferIn'}


def jsonable(value):
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json')
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


class Usage:
    """모델 호출마다 토큰을 모은다 (LangChain 콜백). 에이전트·검증자 모델 모두에 붙인다."""

    def __init__(self):
        from langchain_core.callbacks import BaseCallbackHandler

        rows = self.rows = []

        class Handler(BaseCallbackHandler):
            def on_llm_end(self, response, **kwargs):
                for generations in response.generations:
                    for generation in generations:
                        message = getattr(generation, 'message', None)
                        usage = getattr(message, 'usage_metadata', None) or {}
                        meta = getattr(message, 'response_metadata', None) or {}
                        details = usage.get('input_token_details') or {}
                        rows.append({'model': meta.get('model_name') or meta.get('model'),
                                     'input': usage.get('input_tokens', 0), 'cached': details.get('cache_read', 0) or 0,
                                     'output': usage.get('output_tokens', 0)})
        self.handler = Handler()

    def summary(self, fallback_model: str) -> dict:
        total = {'calls': len(self.rows), 'input': 0, 'cached': 0, 'output': 0, 'usd': Decimal(0), 'priced': True}
        for row in self.rows:
            for key in ('input', 'cached', 'output'):
                total[key] += row[key] or 0
            model = next((name for name in PRICES if row['model'] and str(row['model']).startswith(name)), None) \
                or (fallback_model if fallback_model in PRICES else None)
            if model is None:
                total['priced'] = False
                continue
            price_in, price_cached, price_out = PRICES[model]
            fresh = (row['input'] or 0) - (row['cached'] or 0)
            total['usd'] += (Decimal(fresh) * Decimal(str(price_in)) + Decimal(row['cached'] or 0) * Decimal(str(price_cached))
                             + Decimal(row['output'] or 0) * Decimal(str(price_out))) / Decimal(1_000_000)
        total['usd'] = str(total['usd'].quantize(Decimal('0.0001'))) if total['priced'] else None
        return total


def install_trace(trace: list):
    """등록된 도구 함수를 감싸 이름·인자·결과를 기록한다. 실제로 실행된 호출만 남는다(초안 검증에서 막힌 호출은 빠진다)."""
    from app.agents import tools as T

    for name, tool in T._registry.items():
        original = tool.func

        @functools.wraps(original)
        def wrapped(*args, __original=original, __name=name, **kwargs):
            entry = {'tool': __name, 'args': jsonable({k: v for k, v in kwargs.items() if k != 'runtime'})}
            try:
                result = __original(*args, **kwargs)
            except Exception as error:
                entry['error'] = f'{type(error).__name__}: {error}'
                trace.append(entry)
                raise
            entry['result'] = jsonable(result)
            trace.append(entry)
            return result
        tool.func = wrapped


def install_gate_log(gates: list):
    """초안 검증(DraftGateMiddleware)이 초안 도구를 실행하지 않고 돌려보낸 호출과 지적을 기록한다.
    막힌 호출은 도구 함수가 실행되지 않아 install_trace 기록에 남지 않는다."""
    from app.agents import agents as A

    original_not_run, original_mark = A.DraftGateMiddleware._not_run, A.DraftGateMiddleware._mark

    def not_run(self, call, state, code, issues, lead):
        gates.append({'tool': call['name'], 'state': state, 'code': code, 'args': jsonable(call.get('args')),
                      'issues': jsonable(issues)})
        return original_not_run(self, call, state, code, issues, lead)

    def mark(result, state, issues):
        if state not in ('PASS',):
            gates.append({'state': state, 'issues': jsonable(issues)})
        return original_mark(result, state, issues)
    A.DraftGateMiddleware._not_run = not_run
    A.DraftGateMiddleware._mark = staticmethod(mark)


def install_verify_log(verifies: list):
    """LLM 검증자 호출마다 단계(초안 전·최종 답)·판정·지적을 기록한다. 검증의 개입 횟수와 효과를 보는 데 쓴다."""
    from app.agents import agents as A

    original = A.verify_output

    def verify_output(model, role, attempt, packet):
        stage = 'draft' if 'pending_call' in packet else 'final'
        try:
            result = original(model, role, attempt, packet)
        except Exception as error:
            verifies.append({'stage': stage, 'attempt': attempt, 'verdict': 'ERROR', 'error': type(error).__name__})
            raise
        verifies.append({'stage': stage, 'attempt': attempt, 'verdict': result.get('verdict'),
                         'issues': [{'code': i.get('code'), 'target': i.get('target'), 'explanation': i.get('explanation')}
                                    for i in result.get('issues') or []],
                         'dropped': len(result.get('dropped') or [])})
        return result
    A.verify_output = verify_output


def install_web(fixture):
    """웹 검색을 시나리오 고정 결과로 바꾼다 (실제 검색·홈페이지 수집을 하지 않는다)."""
    from app.features.supplier_crawl.infrastructure import google_search

    def search_suppliers(query, limit=5, **kwargs):
        base = {'query': query, 'search_url': 'https://www.google.com/search', 'suppliers': []}
        if fixture == 'ERROR':
            return base | {'status': 'ERROR', 'code': 'SEARCH_BLOCKED'}
        if not fixture:
            return base | {'status': 'EMPTY'}
        return base | {'status': 'OK', 'suppliers': fixture[:limit], 'fetched_at': '2026-11-20T00:00:00+00:00'}
    google_search.search_suppliers = search_suppliers


def make_models(options: dict, usage: Usage):
    """역할별 에이전트 모델과 LLM 검증자 모델. 이름·추론 수준은 서비스 설정(app/core/config.py)을 따르고,
    --model·--verifier-model을 주면 그 이름으로 바꾼다. 키·주소는 평가 전용 환경변수에서 읽는다."""
    from langchain_openai import ChatOpenAI

    from app.agents.schemas import AgentRole
    from app.core.config import Settings

    settings = Settings  # 클래스 상수만 쓴다(.env를 읽지 않는다)
    key = os.environ['EVAL_OPENAI_API_KEY']
    base_url = os.environ.get('EVAL_OPENAI_BASE_URL', 'https://api.openai.com/v1')

    def chat(name, effort):
        return ChatOpenAI(model=name, base_url=base_url, api_key=key, timeout=settings.llm_timeout_seconds, store=False,
                          callbacks=[usage.handler],
                          **({'use_responses_api': True, 'reasoning': {'effort': effort}} if effort else {}))

    agents, verifiers = {}, {}
    for role in AgentRole:
        name = options['model'] or settings.llm_role_models.get(str(role), settings.llm_model)
        effort = settings.llm_role_reasoning_efforts.get(str(role), settings.llm_reasoning_effort)
        agents[role] = chat(name, effort)
        if not options['no_verifier'] and str(role) in settings.llm_verifier_reasoning_efforts:
            verifiers[role] = chat(options['verifier_model'] or settings.llm_verifier_model,
                                   settings.llm_verifier_reasoning_efforts[str(role)])
    return agents, verifiers


def run_one(task: dict) -> dict:
    """시나리오 한 번 실행. 새 프로세스에서 불린다."""
    sys.path.insert(0, str(ROOT))
    from app.agents import schemas as S
    from app.agents import tools as T
    from app.agents.agents import AgentRunner

    from scripts.agent_eval.scenarios import BY_ID
    from scripts.agent_eval.store import CASE_ID

    sc = BY_ID[task['id']]
    record = {'id': sc.id, 'role': sc.role, 'repeat': task['repeat'], 'title': sc.title}
    usage, trace, gates, verifies = Usage(), [], [], []
    started = time.monotonic()
    try:
        world = sc.world()
        store = world.build()
        extra = sc.inputs(world, store) if sc.inputs else {}
        revision = world.agent_revision(store)
        for setter in (T.use_case_store, T.use_quote_store, T.use_candidate_store, T.use_draft_store, T.use_priority_store):
            setter(store)
        install_trace(trace)
        install_gate_log(gates)
        install_verify_log(verifies)
        install_web(sc.web)
        role = S.AgentRole(sc.role)
        quote_ids = store.latest_ids()
        if sc.role == 'CANDIDATE':
            quote_ids = [world.quote_id(store, '해담과일')]
        data = getattr(S, ROLE_IN[sc.role])(case_id=CASE_ID, input_revision=revision, quote_version_ids=quote_ids, **extra)
        record['input'] = jsonable(data)
        if task['options'].get('dry_plan'):
            record['status'] = 'PLANNED'
            return record
        agents, verifiers = make_models(task['options'], usage)
        runner = AgentRunner(agents[role], role_models=agents, verifiers=verifiers)
        output = runner(role, data)
        record['status'] = 'OK'
        record['output'] = jsonable(output)
    except Exception as error:
        record['status'] = 'ERROR'
        record['error'] = f'{type(error).__name__}: {error}'
        # API 오류는 상태 코드·응답 본문을 남긴다(langchain이 감싼 메시지에는 "Error code: 400"만 남을 때가 있다)
        cause = error
        while cause is not None and getattr(cause, 'status_code', None) is None:
            cause = cause.__cause__ or cause.__context__
        if cause is not None:
            response = getattr(cause, 'response', None)
            record['api_error'] = {'status': getattr(cause, 'status_code', None), 'body': jsonable(getattr(cause, 'body', None)),
                                   'text': (getattr(response, 'text', '') or '')[:2000],
                                   'request_id': getattr(response, 'headers', {}).get('x-request-id') if response is not None else None}
            record['error'] += f" | API {record['api_error']['status']} {str(record['api_error']['body'] or record['api_error']['text'])[:300]}"
        record['traceback'] = traceback.format_exc()[-3000:]
    finally:
        record['seconds'] = round(time.monotonic() - started, 2)
        record['tools'] = trace
        record['draft_gate'] = gates
        record['verifier'] = verifies
        if 'store' in locals():
            record['drafts'] = [{'id': draft_id, 'purpose': d.purpose, 'supplier_id': d.supplier_id, 'subject': d.subject,
                                 'body': d.body_text} for draft_id, d in store.drafts.items()]
            record['store_calls'] = [[name, jsonable(args)] for name, args in store.calls]
            record['names'] = {sid: row['name'] for sid, row in store.suppliers.items()}
        record['usage'] = jsonable(usage.summary(task['options'].get('model') or ''))
    return record


def main():
    parser = argparse.ArgumentParser(description='②~⑤ 에이전트 평가 실행 (B 방식)')
    parser.add_argument('--only', nargs='*', help='시나리오 ID (예: C-01 P-02). 없으면 전체')
    parser.add_argument('--roles', nargs='*', choices=['C', 'P', 'R', 'O'], help='역할 접두사로 고르기')
    parser.add_argument('--repeat', type=int, default=1, help='시나리오당 반복 횟수 (흔들림 확인)')
    parser.add_argument('--workers', type=int, default=4, help='동시에 돌릴 프로세스 수 (API 속도 제한에 맞춘다)')
    parser.add_argument('--model', help='에이전트 모델을 모든 역할에 이 이름으로 (기본: 서비스 설정 그대로)')
    parser.add_argument('--verifier-model', help='LLM 검증자 모델 (기본: 서비스 설정 그대로)')
    parser.add_argument('--no-verifier', action='store_true', help='LLM 검증자 없이 (서비스와 다름. 비교용)')
    parser.add_argument('--out', default='eval_results', help='결과 폴더')
    parser.add_argument('--dry-plan', action='store_true', help='모델을 부르지 않고 입력만 만들어 본다')
    parser.add_argument('--langsmith', action='store_true', help='LangSmith 기록 켜기 (팀 프로젝트에 기록된다)')
    parser.add_argument('--budget', type=float, help='추정 비용(USD)이 이 값에 닿으면 새 시나리오를 시작하지 않고 멈춘다')
    args = parser.parse_args()

    if args.langsmith:
        os.environ['LANGSMITH_TRACING'] = 'true'
    if not args.dry_plan and not os.environ.get('EVAL_OPENAI_API_KEY'):
        sys.exit('EVAL_OPENAI_API_KEY 환경변수에 평가용 API 키를 넣어 주세요. (서버 .env의 LLM_API_KEY는 읽지 않습니다)')

    from scripts.agent_eval.scenarios import SCENARIOS
    chosen = [s for s in SCENARIOS if (not args.only or s.id in args.only) and (not args.roles or s.id[0] in args.roles)]
    if args.only:
        missing = sorted(set(args.only) - {s.id for s in chosen})
        if missing:
            sys.exit(f'없는 시나리오: {missing}')
    options = {'model': args.model, 'verifier_model': args.verifier_model, 'no_verifier': args.no_verifier,
               'dry_plan': args.dry_plan}
    tasks = [{'id': s.id, 'repeat': r, 'options': options} for r in range(1, args.repeat + 1) for s in chosen]

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    out_dir = ROOT / args.out / f'{stamp}{"-dry" if args.dry_plan else ""}'
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        commit = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = None
    from app.agents.agents import system_prompt
    from app.agents.schemas import AgentRole
    import hashlib
    meta = {'started': stamp, 'commit': commit, 'options': options, 'repeat': args.repeat,
            'scenarios': [s.id for s in chosen],
            'prompt_versions': {str(r): hashlib.sha256(system_prompt(r).encode()).hexdigest()[:12] for r in AgentRole}}
    (out_dir / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(chosen)}개 시나리오 × {args.repeat}회 = {len(tasks)}건 → {out_dir}')

    done, failed, cost = 0, 0, Decimal(0)
    context = multiprocessing.get_context('spawn')
    with context.Pool(args.workers, maxtasksperchild=1) as pool, open(out_dir / 'runs.jsonl', 'a', encoding='utf-8') as sink:
        for record in pool.imap_unordered(run_one, tasks):
            sink.write(json.dumps(record, ensure_ascii=False) + '\n')
            sink.flush()
            done += 1
            failed += record['status'] == 'ERROR'
            cost += Decimal(record['usage'].get('usd') or 0)
            action = (record.get('output') or {}).get('next_action') or record['status']
            print(f'[{done}/{len(tasks)}] {record["id"]}#{record["repeat"]} {action} {record["seconds"]}s ~${cost:.2f}'
                  + (f'  {record.get("error", "")[:200]}' if record['status'] == 'ERROR' else ''))
            if args.budget is not None and cost >= Decimal(str(args.budget)):
                print(f'\n추정 비용 ${cost:.2f}가 상한 ${args.budget:.2f}에 닿아 멈춥니다. 진행 중이던 시나리오는 기록되지 않습니다.')
                pool.terminate()
                break
            if done >= 4 and failed == done:
                print('\n처음 4건이 모두 실행 오류라 멈춥니다. 위 오류(키·주소·모델 이름)를 먼저 확인하세요.')
                pool.terminate()
                break
    print(f'\n완료 {done}건 (실행 오류 {failed}) · 추정 비용 ${cost:.2f}\n채점: python -m scripts.agent_eval.grade {out_dir}')


if __name__ == '__main__':
    main()
