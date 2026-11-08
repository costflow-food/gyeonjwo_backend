"""Reproducible offline 1→5 integration; no database writes or email sends.

python -m scripts.check_market_scenario
Uses real tools, final checks, graph, review storage validation and API-14 mapping.
The deterministic role driver is not a live LLM or a live DB E2E test.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from agent_memory_store import MemoryStore
from langchain_core.messages import ToolMessage

from app.agents import agents, graph, nodes, schemas as s, tools
from app.features.work_cases.application.market_reference import MarketReferenceService, KST
from app.features.work_cases.application.review_card import ReviewCardService
from app.features.work_cases.infrastructure.review_graph_store import ReviewGraphStore


def uid(label):
    return str(uuid5(NAMESPACE_URL, 'gyeonjwo-market-e2e-v1:' + label))


CASE, INITIAL = uid('case'), uid('initial')
ITEM, SPEC = '깐마늘', '국산 대서 상품, 20kg 포장'
SUPPLIERS = {key: uid(key) for key in ('incumbent', 'a', 'b', 'c', 'excluded')}
NAMES = {'incumbent': '시연마늘농산', 'a': '시연대서유통', 'b': '시연깐마늘식품', 'c': '시연알마늘',
         'excluded': '시연미확인농산'}

# Source: user-supplied successful public API response, 2026-11-08; not a supplier quote.
PUBLIC_ROW = {'item_cd': '258', 'vrty_cd': '03', 'se_cd': '02', 'grd_cd': '04',
              'item_nm': '깐마늘(국산)', 'vrty_nm': '깐마늘(대서)', 'se_nm': '중도매', 'grd_nm': '상품',
              'exmn_ymd': '20261106', 'exmn_dd_prc': '179200', 'unit_sz': '20', 'unit': 'kg'}


class ScenarioStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.role_order, self.role_runs, self.role_outputs, self.cards = [], {}, {}, {}
        self.scope = SimpleNamespace(revision=1)
        self.attachment_bytes, self.documents = {}, {}

    def list_unread_attachments(self, case_id, mail_ids):
        return [{'mail_id': mid, **a} for mid in mail_ids for a in self.mail_messages[mid]['attachments']
                if (mid, a['attachment_ref']) not in self.documents]

    def download_attachment(self, case_id, mail_id, attachment_ref):
        return self.attachment_bytes[(mail_id, attachment_ref)]

    def save_document(self, case_id, mail_id, attachment_ref, document):
        self.documents[(mail_id, attachment_ref)] = document
        for a in self.mail_messages[mail_id]['attachments']:
            if a['attachment_ref'] == attachment_ref:
                a['pages'] = document['pages']

    def latest_ids(self, case_id):
        return self.get_work_case(case_id)['latest_quote_version_ids']

    def check(self, case_id):
        assert case_id == CASE

    def insert_quote_version(self, record, revision):
        result = super().insert_quote_version(record, revision)
        if result and result[0].startswith('qv-'):
            old, saved = result
            key = uid(old)
            self.quote_versions[key] = self.quote_versions.pop(old)
            self.price_history[-1]['quote_version_id'] = key
            return key, saved
        return result

    def start_agent_run(self, *args, **kwargs):
        started = super().start_agent_run(*args, **kwargs)
        old = started['agent_run_id']
        key = uid(old)
        self.agent_runs[key] = self.agent_runs.pop(old)
        self.role_order.append(key)
        self.role_runs[key] = str(args[1])
        return {**started, 'agent_run_id': key}

    def finish_agent_run(self, run_id, status, output, error):
        super().finish_agent_run(run_id, status, output, error)
        if status == 'COMPLETED':
            self.role_outputs[run_id] = output

    def save_review_card(self, case_id, revision, card):
        return ReviewGraphStore.save_review_card(self, case_id, revision, card)

    def rpc(self, name, **kwargs):
        assert name == 'stage_case_graph_review_card'
        card = kwargs['p_card']
        assert 1 <= len(card['plans']) <= 5
        assert all(self.quote_versions[a['quote_version_id']].unit == 'kg'
                   for p in card['plans'] for a in p['allocations'])
        self.cards[uid('card')] = card
        return {'kind': 'STAGED', 'card_id': uid('card')}


def setup():
    store = ScenarioStore()
    store.add_case(CASE, item_name=ITEM, specification=SPEC, quantity='1000', unit='kg', mandatory_conditions=['HACCP'],
                   policy={'allow_split': True, 'max_suppliers': 2,
                           'max_increase_rate': '0.04', 'incumbent_premium_rate': '0.04'})
    store.scope.batch_id = store.work_cases[CASE]['review_batch_id']
    for key, sid in SUPPLIERS.items():
        store.add_supplier(sid, NAMES[key], f'{key}@example.invalid')
        store.add_supplier_item(uid('item-' + key), sid, ITEM, specification=SPEC, origin='국산',
                                certifications=[] if key == 'excluded' else ['HACCP'])
    store.add_incumbent(CASE, SUPPLIERS['incumbent'])
    for key, price, quantity in [('incumbent', '9400', '1000'), ('a', '9700', '300'), ('c', '9500', '200')]:
        store.add_purchase(uid('purchase-' + key), uid('item-' + key), price, 'kg', '2026-10-15', quantity=quantity)
    return store


def mail(store, key, price, quantity='1000', shipping='무료', *, initial=False, clarification=False, attachments_dir=None):
    mid = INITIAL if initial else uid(key + ('-clarification' if clarification else '-reply'))
    values = {'supplier_name': NAMES[key], 'item_name': ITEM, 'specification': SPEC,
              'unit_price': f'{price}원', 'unit': 'kg', 'available_quantity': f'{quantity}kg',
              'tax_terms': '부가세 면세', 'moq': '100kg', 'pack_multiple': '20kg', 'certifications': 'HACCP',
              'origin': '국산', 'effective_from': '2026-11-15', 'valid_until': '2026-12-31', 'lead_time': '발주 후 3일'}
    if initial:
        values |= {'previous_unit_price': '9400원', 'increase_reason': '원물 매입비 상승'}
    if clarification:
        values = {'supplier_name': NAMES[key]}
    labels = {'supplier_name': '업체명', 'item_name': '품목', 'specification': '규격',
              'unit_price': '변경 단가' if initial else '견적 단가', 'unit': '단가 기준 단위',
              'available_quantity': '공급 가능량', 'tax_terms': '세금 조건', 'moq': '최소 주문량',
              'pack_multiple': '발주 배수', 'previous_unit_price': '기존 단가', 'increase_reason': '인상 사유'}
    labels |= {'certifications': '인증', 'origin': '원산지', 'effective_from': '적용일',
               'valid_until': '견적 유효기간', 'lead_time': '납품 소요일'}
    body = '\n'.join(f'{labels[k]}: {v}' for k, v in values.items()) + f'\n운송비: {shipping}'
    if shipping.isdigit():
        body += '원, 1회 배송당 별도'
    source_body, attachments = body, []
    evidence = tools.EvidenceRef(mail_id=mid)
    if attachments_dir:
        filename = f'{key}_{"clarification" if clarification else "quote"}.pdf'
        content = (Path(attachments_dir) / filename).read_bytes()
        store.attachment_bytes[(mid, filename)] = content
        attachments = [{'attachment_ref': filename, 'filename': filename, 'content_type': 'application/pdf',
                        'size_bytes': len(content), 'pages': []}]
        body = f'안녕하세요, {NAMES[key]}입니다. 요청하신 안내문을 첨부드립니다. 확인 부탁드립니다.'
        evidence = tools.EvidenceRef(mail_id=mid, part=filename, page=1)
    store.add_mail(mid, CASE, body, supplier_id=SUPPLIERS[key], attachments=attachments,
                   message_role='INITIAL' if initial else 'REPLY')
    fields = [tools.QuoteField(field_name=name, raw_value=value, evidence=evidence,
                              **({'tax_basis': 'EXEMPT'} if name == 'tax_terms' else {}))
              for name, value in values.items()]
    cost = tools.CostTerm(cost_type='SHIPPING', source_text=source_body.split('\n')[-1],
        amount=None if shipping == '추후 안내' else shipping if shipping == '무료' else f'{shipping}원',
        basis=None if shipping in ('무료', '추후 안내') else '1회 배송당',
        included_in_price=False, evidence=evidence)
    return mid, fields, [cost]


class RoleDriver:
    """Select deterministic actions; all facts and amounts come from production tools."""
    def __init__(self, store):
        self.store, self.inputs = store, {}

    def __call__(self, role, data):
        started = self.store.start_agent_run(CASE, role, 1, data.model_dump(mode='json'), 'deterministic', 'scenario-v1')
        ctx = s.RunContext(CASE, 1, started['agent_run_id'], started['review_batch_id'])
        messages = []
        def record(name, result):
            messages.append(ToolMessage(content=json.dumps(result, ensure_ascii=False), name=name, tool_call_id=name))
            return result
        if role == s.AgentRole.INFO_EXTRACTOR:
            ids = []
            for mid in data.mail_ids:
                fields, costs = self.inputs[mid]
                result = record('store_structured_mail', tools.save_structured_quote(self.store, ctx, fields, [], costs, []))
                assert result['status'] == 'STORED', result
                ids.append(result['quote_version_id'])
            out = s.ExtractionOut(next_action='STORED', reason='원문 견적을 저장했습니다.', quote_version_ids=ids)
        elif role == s.AgentRole.CANDIDATE:
            result = record('search_supplier_evidence', tools.evaluate_candidates(self.store, ctx,
                            [uid('item-' + key) for key in SUPPLIERS]))
            assert {c['supplier_id'] for c in result['candidates']} == {SUPPLIERS[k] for k in ('a', 'b', 'c')}
            drafted = record('create_rfq_draft', tools.save_mail_drafts(self.store, ctx, 'RFQ',
                           [uid('item-' + key) for key in ('a', 'b', 'c')], [SUPPLIERS[k] for k in ('a', 'b', 'c')]))
            assert drafted['status'] == 'DRAFTED', drafted
            out = s.CandidateOut(next_action='SEND_RFQ', reason='등록 후보에게 보낼 견적 요청을 준비했습니다.',
                candidates=[s.Candidate(supplier_id=c['supplier_id'], reason='등록된 동일 규격 취급 업체입니다.')
                            for c in result['candidates']],
                draft_ids=[d['draft_id'] for d in drafted['drafts']])
        elif role == s.AgentRole.INFO_PRIORITY:
            compared = record('check_comparability', tools.compare_quotes(self.store, ctx))
            if compared['break_even_needed']:
                sid = SUPPLIERS['b']
                threshold = record('calculate_break_even', tools.break_even_for(self.store, ctx, sid))
                assert threshold['deciding']['break_even_amount'] == '100000', threshold
                assert all(r['rival_supplier_id'] != SUPPLIERS['c'] for r in threshold['results'])
                drafted = record('create_clarification_draft', tools.save_clarification_drafts(self.store, ctx,
                    [tools.ClarificationRequest(supplier_id=sid, fields=['cost:SHIPPING'])]))
                assert drafted['status'] == 'DRAFTED', drafted
                out = s.PriorityOut(next_action='REQUEST_CLARIFICATION', reason='운송비를 확인하면 선택이 달라질 수 있습니다.',
                    questions=[s.Question(supplier_id=sid, field='cost:SHIPPING', priority='CAN_CHANGE_CHOICE',
                                          reason='운송비가 원문에서 미확인입니다.')],
                    draft_ids=[d['draft_id'] for d in drafted['drafts']])
            else:
                assert not any(row['missing_required'] for row in compared['suppliers']), compared
                out = s.PriorityOut(next_action='BUILD_PLANS', reason='동일 조건으로 비교할 수 있습니다.')
        elif role == s.AgentRole.RESPONSE_PLAN:
            found = tools.plan_candidates(self.store, ctx, [])
            plans = []
            for p in found['plans'][:5]:
                if p['status'] not in {'VALID', 'CONDITIONAL'}:
                    continue
                allocations = [tools.PlanAllocationInput(supplier_id=a['supplier_id'], quantity=a['quantity'])
                               for a in p['allocations']]
                result = record('validate_purchase_plan', tools.validate_plan(self.store, ctx, p['plan_type'], allocations, 'quantities', []))
                plans.append(s.Plan(plan_type=p['plan_type'], allocations=result['allocations'],
                                    total_amount=result['total_amount'], validation_status=result['status']))
            out = s.PlanOut(next_action='PLANS_PROPOSED', reason='유지·전환·분산안을 비교했습니다.', plans=plans)
        else:
            best = min((p for p in data.validated_plans if p.plan_type != 'KEEP'), key=lambda p: int(p.total_amount))
            alternative = [tools.AlternativeAllocation(**a.model_dump()) for a in best.allocations]
            numbers = record('calculate_counter_offer', tools.counter_offer_numbers(self.store, ctx, alternative))
            if numbers['counter_status'] == 'NO_POLICY':
                out = s.CounterOfferOut(next_action='HOLD', reason='허용 인상률 정책을 확인해야 합니다.')
            else:
                target = numbers['allowed_range'][1]
                drafted = record('create_counter_offer_draft', tools.save_counter_offer_draft(self.store, ctx, target, [], alternative))
                assert drafted['status'] == 'DRAFTED', drafted
                out = s.CounterOfferOut(next_action='COUNTER_OFFER_PROPOSED', reason='정책과 검증된 대안을 기준으로 역제안합니다.',
                    target=s.CounterOfferTarget(supplier_id=SUPPLIERS['incumbent'], target_unit_price=target))
        checked = agents.check_final_output(role, data, ctx, out, messages)
        self.store.finish_agent_run(started['agent_run_id'], 'COMPLETED', checked.model_dump(mode='json'), None)
        return checked


def run_scenario(no_policy=False, live_settings=None, market=None, attachments_dir=None):
    store = setup()
    if market is None:
        market = MarketReferenceService()
        market._load = lambda: ([PUBLIC_ROW], datetime(2026, 11, 8, tzinfo=KST))
    if no_policy:
        store.work_cases[CASE]['policy'].pop('max_increase_rate')
    runner = (agents.AgentRunner(agents.chat_model(live_settings), runs=store,
                                role_models=agents.role_models(live_settings)) if live_settings else RoleDriver(store))
    hooks = (tools.use_case_store, tools.use_quote_store, tools.use_candidate_store,
             tools.use_draft_store, tools.use_priority_store, nodes.use_document_store)
    for hook in hooks:
        hook(store)
    app = graph.build_graph(runner, store.current_input_revision,
        nodes=nodes.Nodes(review_card=lambda state: {
            'card_id': store.save_review_card(CASE, 1, nodes.build_review_card(state).model_dump(mode='json'))}),
        latest_quotes=store.latest_ids)
    rounds = []
    def entry(key, price, **kwargs):
        return mail(store, key, price, attachments_dir=attachments_dir, **kwargs)
    def run(mode, entries):
        for mid, fields, costs in entries:
            if isinstance(runner, RoleDriver):
                runner.inputs[mid] = (fields, costs)
        result = graph.run_case(app, s.RunCaseInput(case_id=CASE, input_revision=1, start_from=mode,
                                                   mail_ids=[e[0] for e in entries]))
        rounds.append(result.model_dump(mode='json'))
        return result
    try:
        assert run('START', [entry('incumbent', '10340', initial=True)]).status == 'WAITING_REPLY'
        assert run('REPLY', [entry('a', '9700'), entry('b', '9600', shipping='추후 안내'),
                            entry('c', '9500', quantity='600')]).status == 'WAITING_REPLY'
        final = run('REPLY', [entry('b', '9600', shipping='70000', clarification=True)])
        assert final.status == 'COMPLETE', final
        case = store.get_work_case(CASE) | {'id': CASE, 'review_revision': 1, 'status': 'Complete',
                                          'latest_review_jsonb': store.cards[uid('card')]}
        def quotes(owner, case_id, ids):
            return {qid: row.model_dump() for qid, row in store.get_quote_versions(case_id, ids)}
        repo = SimpleNamespace(case=lambda *_: case, quotes=quotes,
                               suppliers=lambda owner, ids: {sid: store.suppliers[sid]['name'] for sid in ids})
        response = ReviewCardService(repo, market).get('offline-owner', CASE, changes_ready=True)
        return {'mode': 'live_llm_memory_store' if live_settings else 'offline_graph_and_api_contract', 'item': ITEM, 'specification': SPEC,
                'rounds': rounds, 'card': response.model_dump(mode='json'),
                'documents': [{'mail_id': mid, 'attachment_ref': ref, 'status': doc['status'], 'page_count': len(doc['pages'])}
                              for (mid, ref), doc in store.documents.items()],
                'price_evidence': [q.evidence_jsonb.get('unit_price') for q in store.quote_versions.values()],
                'fixture': {'synthetic': True, 'case': store.work_cases[CASE],
                            'suppliers': list(store.suppliers.values()), 'supplier_items': list(store.supplier_items.values()),
                            'purchase_history': list(store.purchase_history.values()),
                            'mails': [{'id': mid, **{k: v for k, v in row.items() if k != 'normalized_jsonb'}}
                                      for mid, row in store.mail_messages.items()]},
                'draft_purposes': [d.purpose for d in store.mail_drafts.values()]}
    finally:
        for hook in hooks:
            hook(None)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-llm', action='store_true', help='Calls the configured LLM; still no DB writes or sending.')
    parser.add_argument('--live-market', action='store_true', help='Read public API using DATA_GO_KR_API_KEY instead of the saved response.')
    parser.add_argument('--fixture-output', type=Path, help='Save the synthetic case, suppliers and five mail bodies as JSON (no DB writes).')
    parser.add_argument('--attachments-dir', type=Path, help='Use the five quote PDFs and real document preprocessing instead of body quotes.')
    parser.add_argument('--env', type=Path)
    args = parser.parse_args()
    settings, market = None, None
    if args.live_llm or args.live_market:
        from app.core.config import Settings
        settings = Settings(_env_file=args.env) if args.env else Settings()
    if args.live_llm:
        settings.require_llm()
    if args.live_market:
        key = settings.data_go_kr_api_key
        market = MarketReferenceService(key.get_secret_value() if key else None)
    result = run_scenario(live_settings=settings if args.live_llm else None, market=market, attachments_dir=args.attachments_dir)
    result['market_mode'] = 'public_api' if args.live_market else 'user_response_snapshot_2026-11-06'
    fixture = result.pop('fixture')
    if args.fixture_output:
        args.fixture_output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
