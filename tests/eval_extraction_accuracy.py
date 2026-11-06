"""① 추출 정확도 평가 (KAN-186 X-45, 03 §6·AIR-11). 정답이 있는 PDF 문서로 ①을 N회 실행하고
필드 정확도·함수 통과율·지연·비용을 따로 기록한다. 합성 문서, 실제 거래 아님.

  python tests/eval_extraction_accuracy.py --dry                       LLM 없이 PDF 읽기·정답 형식만 확인
  RUN_LIVE_LLM=1 python tests/eval_extraction_accuracy.py --runs 3 --out <폴더>   실제 LLM (비용 발생)
      [--only haedam,miso] [--price-in 0.4 --price-out 1.6]   (USD / 100만 토큰, 주면 비용을 계산)

- 필드 정확도: 정답 JSON의 필드(row·costs·tax_basis·case) 중 저장 값과 같은 비율. 저장하지 못한 회차는 모두 틀림.
- 함수 통과율: store_structured_mail 호출 중 STORED로 끝난 비율 (거절 뒤 재전송도 호출 1회로 센다).
- 지연: 회차별 ① 실행 시간(초). 비용: 입력·출력 토큰, 단가를 주면 USD.
"""
import argparse
import json
import os
import statistics
import sys
import time
import unicodedata
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from agent_memory_store import MemoryStore  # noqa: E402
from app.agents import tools  # noqa: E402
from app.agents.agents import AgentRunner, build_agent  # noqa: E402
from app.agents.nodes import read_document  # noqa: E402
from app.agents.schemas import AgentRole, ExtractionIn  # noqa: E402

FIXTURES = ROOT / 'tests' / 'fixtures' / 'mango'
CASE = '00000000-0000-4000-8000-000000000186'
MAIL = 'mail-1'


def fixture(name):
    """파일명의 한글이 NFC·NFD 어느 쪽으로 저장돼도 찾는다 (미소농산 PDF는 NFD로 저장돼 있다)."""
    nfc = unicodedata.normalize('NFC', name)
    return next(p for p in FIXTURES.iterdir() if unicodedata.normalize('NFC', p.name) == nfc)


def load_documents(only=None):
    docs = json.loads((FIXTURES / 'answers.json').read_text(encoding='utf-8'))['documents']
    docs = [d for d in docs if not only or d['id'] in only]
    for doc in docs:
        result = read_document(doc['file'], 'application/pdf', fixture(doc['file']).read_bytes())
        assert result['status'] == 'READ' and any(result['pages']), f"{doc['file']}: {result['status']}"
        doc['pages'] = result['pages']
    return docs


def make_store(doc):
    supplier_id, name = doc['supplier']
    memory = MemoryStore()
    quantity, unit = doc['case_quantity'] or (None, None)
    memory.add_case(CASE, item_name='냉동 망고', specification='IQF 다이스 10kg/박스', quantity=quantity, unit=unit)
    memory.add_supplier(supplier_id, name, f'{supplier_id}@example.com')
    attachment = {'attachment_ref': 'att-1', 'filename': doc['file'], 'pages': doc['pages']}
    memory.add_mail(MAIL, CASE, f'{name}입니다. {doc["file"].rsplit(".", 1)[0]} 첨부드립니다.', supplier_id=supplier_id,
                    attachments=[attachment], message_role=doc['message_role'], subject=f'[{name}] 냉동 망고')
    return memory


def _same(actual, expected):
    if expected is None or actual is None:
        return actual is None and expected is None
    try:
        return Decimal(str(actual)) == Decimal(str(expected))
    except ArithmeticError:
        return str(actual) == str(expected)


def score_fields(doc, memory, row):
    """{필드: {expected, actual, ok}}. 저장 행이 없으면 모든 필드가 actual None으로 틀린다."""
    costs = {c.get('cost_type'): c for c in (row.cost_terms_jsonb if row else [])}
    fields = {f'row.{k}': (getattr(row, k) if row else None, v) for k, v in doc['row'].items()}
    for kind, want in doc['costs'].items():
        if 'included' in want:
            fields[f'costs.{kind}.included'] = ((costs.get(kind) or {}).get('included_in_price'), want['included'])
        if 'amount' in want:
            fields[f'costs.{kind}.amount'] = ((costs.get(kind) or {}).get('amount'), want['amount'])
    fields['tax_basis'] = (tools.tax_basis(row) if row else None, doc['tax_basis'])
    case = memory.work_cases[CASE]
    fields |= {f'case.{k}': (case.get(k) if row else None, v) for k, v in doc['case'].items()}
    out = {}
    for name, (actual, expected) in fields.items():
        ok = actual == expected if isinstance(expected, bool) else _same(actual, expected)
        out[name] = {'expected': expected, 'actual': None if actual is None else str(actual), 'ok': row is not None and ok}
    return out


def usage_and_calls(messages):
    usage, stores = {'input_tokens': 0, 'output_tokens': 0, 'llm_calls': 0}, []
    for message in messages:
        if isinstance(message, AIMessage):
            usage['llm_calls'] += 1
            for key in ('input_tokens', 'output_tokens'):
                usage[key] += (message.usage_metadata or {}).get(key, 0)
        elif isinstance(message, ToolMessage) and message.name == 'store_structured_mail':
            try:
                result = json.loads(message.content)
                codes = ','.join(f"{r.get('field')}:{r.get('code')}" for r in result.get('rejected') or [])
                stores.append(result.get('status') + (f'[{codes}]' if codes else ''))   # 'REJECTED[unit:SOURCE_MISMATCH]'
            except (TypeError, ValueError, AttributeError):
                stores.append('UNREADABLE')
    return usage, stores


def run_once(doc, model, role_models):
    memory = make_store(doc)
    tools.use_case_store(memory)
    tools.use_quote_store(memory)
    agent, seen = build_agent(AgentRole.INFO_EXTRACTOR, model), {}

    class Recording:
        def invoke(self, payload, context=None, config=None):
            result = agent.invoke(payload, context=context, config=config)
            seen['messages'] = result['messages']
            return result

    runner = AgentRunner(model, runs=memory, role_models=role_models)
    runner.agents[AgentRole.INFO_EXTRACTOR] = Recording()
    started, output, error = time.perf_counter(), None, None
    try:
        output = runner(AgentRole.INFO_EXTRACTOR, ExtractionIn(case_id=UUID(CASE), input_revision=1, mail_ids=[MAIL]))
    except Exception as exc:        # 실패도 회차 결과로 남긴다
        error = f'{type(exc).__name__}: {exc}'[:300]
    finally:
        tools.use_case_store(None)
        tools.use_quote_store(None)
    latency = time.perf_counter() - started
    usage, stores = usage_and_calls(seen.get('messages', []))
    ids = output.quote_version_ids if output else []
    row = memory.quote_versions.get(ids[-1]) if ids else None
    return {'action': output.next_action if output else 'ERROR', 'error': error, 'latency_s': round(latency, 2),
            'usage': usage, 'store_calls': stores, 'fields': score_fields(doc, memory, row)}


def summarize(doc, runs, price_in, price_out):
    fields = [f for r in runs for f in r['fields'].values()]
    calls = [s for r in runs for s in r['store_calls']]
    tokens_in = sum(r['usage']['input_tokens'] for r in runs)
    tokens_out = sum(r['usage']['output_tokens'] for r in runs)
    cost = (tokens_in * price_in + tokens_out * price_out) / 1e6 if price_in is not None else None
    return {
        'id': doc['id'], 'runs': len(runs),
        'action_ok': sum(r['action'] == doc['expect'] for r in runs),
        'field_accuracy': round(sum(f['ok'] for f in fields) / len(fields), 3),
        'field_accuracy_by_run': [round(sum(f['ok'] for f in r['fields'].values()) / len(r['fields']), 3) for r in runs],
        'function_pass_rate': round(calls.count('STORED') / len(calls), 3) if calls else None,
        'store_calls': len(calls),
        'latency_s': {'mean': round(statistics.mean(r['latency_s'] for r in runs), 2),
                      'max': max(r['latency_s'] for r in runs)},
        'tokens': {'input': tokens_in, 'output': tokens_out}, 'cost_usd': cost and round(cost, 4),
        'wrong_fields': sorted({n for r in runs for n, f in r['fields'].items() if not f['ok']}),
    }


def self_check():
    """LLM 없이: PDF가 읽히고 정답 필드가 비교 함수에서 맞게 판정되는지."""
    docs = load_documents()
    assert [d['id'] for d in docs] == ['haedam', 'dahae', 'miso']
    assert _same('5500.00', '5500') and _same(None, None) and not _same('0', None) and not _same('5150', '5200')
    for doc in docs:
        memory = make_store(doc)
        fields = score_fields(doc, memory, None)
        assert fields and not any(f['ok'] for f in fields.values())     # 저장 없음 → 모두 틀림
        assert doc['row']['unit_price'] in ''.join(doc['pages']).replace(',', '')     # 정답 단가가 원문에 있다
        print(doc['id'], doc['title'], f'필드 {len(fields)}개', f"쪽 {len(doc['pages'])}")
    print('OK')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry', action='store_true')
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--only')
    parser.add_argument('--out', default=str(ROOT / '.local-tmp' / 'extraction_accuracy'))
    parser.add_argument('--price-in', type=float)
    parser.add_argument('--price-out', type=float)
    args = parser.parse_args()
    if args.dry:
        return self_check()
    if not os.getenv('RUN_LIVE_LLM'):
        sys.exit('실제 LLM을 호출합니다(비용 발생). RUN_LIVE_LLM=1일 때만 실행합니다. LLM 없이 확인: --dry')
    from app.agents.agents import chat_model, role_models
    from app.core.config import get_settings
    settings = get_settings()
    model, models = chat_model(settings), role_models(settings)
    docs = load_documents(set(args.only.split(',')) if args.only else None)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summaries = []
    for doc in docs:
        runs = []
        for n in range(1, args.runs + 1):
            runs.append(run_once(doc, model, models))
            print(f"{doc['id']} {n}/{args.runs}: {runs[-1]['action']} {runs[-1]['latency_s']}s "
                  f"calls={runs[-1]['store_calls']}", flush=True)
        summary = summarize(doc, runs, args.price_in, args.price_out)
        summaries.append(summary)
        (out / f"{doc['id']}.json").write_text(json.dumps({'summary': summary, 'runs': runs}, ensure_ascii=False,
                                                          indent=2), encoding='utf-8')
    meta = {'model': settings.llm_model, 'runs': args.runs, 'documents': summaries}
    (out / 'summary.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
