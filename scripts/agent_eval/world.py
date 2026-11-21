"""시나리오 데이터 빌더. 업체·등록 품목·거래 이력·견적 메일을 짧게 적으면 MemoryStore를 만들어 준다.

견적은 손으로 QuoteVersionRecord를 만들지 않는다. 메일 원문을 만들고 ①의 저장 함수(save_structured_quote)에
원문 근거와 함께 넣어 저장한다. 그래서 견적 행의 형식(비용·세금·이어받기·업무 조건 채우기)이 서비스에서 ①이
저장한 것과 같다. 저장이 거절되면 시나리오 데이터가 잘못된 것이므로 예외로 멈춘다.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from app.agents.schemas import RunContext
from app.agents.tools import CostTerm, EvidenceRef, ItemNote, QuoteField, save_structured_quote

from scripts.agent_eval.store import CASE_ID, MemoryStore

ITEM = '냉동 망고'
SPEC = 'IQF 다이스 10kg/박스'


# 등록 데이터 (② 후보 추천)

@dataclass
class Supplier:
    key: str                      # 시나리오에서 부르는 이름 (보통 업체명과 같다)
    name: str | None = None
    email: bool = True
    web: bool = False             # 웹 수집 등록 업체 (suppliers.source_type = VERIFIED_WEB)

    @property
    def id(self) -> str:
        return f's-{self.key}'


@dataclass
class Item:
    supplier: str                 # Supplier.key
    item_name: str = ITEM
    specification: str | None = SPEC
    certifications: list | None = field(default_factory=lambda: ['HACCP'])   # None: 미확인, []: 보유 인증 없음
    origin: str | None = '베트남'
    notes: str | None = None
    history: list[tuple[str, int]] = field(default_factory=list)   # [(거래일 'YYYY-MM-DD', kg당 단가)]
    suffix: str = ''              # 같은 업체의 품목이 둘 이상일 때 ID 구분

    @property
    def id(self) -> str:
        return f'i-{self.supplier}{self.suffix}'


# 견적 메일 (③④⑤ 및 ②의 인상 통보)

@dataclass
class Cost:
    """비용 한 줄. kind: SHIPPING·HANDLING / mode:
    included(단가 포함) · free(무료) · none(없음) · per_order(회당 금액 별도) · per_box(박스당 금액 별도) ·
    unpriced(금액 없이 "별도")."""
    kind: str = 'SHIPPING'
    mode: str = 'included'
    amount: str | None = None     # '100,000원'
    text: str | None = None       # 원문 구절을 직접 정할 때

    def phrase(self) -> str:
        label = {'SHIPPING': '운송비', 'HANDLING': '하차비'}[self.kind]
        if self.text:
            return self.text
        return {'included': f'{label}는 단가에 포함됩니다' if self.kind == 'SHIPPING' else f'{label}는 단가에 포함됩니다',
                'free': f'{label} 무료',
                'none': f'{label} 없음',
                'per_order': f'{label} 회당 {self.amount} 별도',
                'per_box': f'{label} 박스당 {self.amount} 별도',
                'unpriced': f'{label} 별도'}[self.mode]

    def term(self, mail_id: str) -> CostTerm:
        evidence = EvidenceRef(mail_id=mail_id)
        source = self.phrase()
        if self.mode == 'included':
            return CostTerm(cost_type=self.kind, source_text=source, included_in_price=True, evidence=evidence)
        if self.mode in ('free', 'none'):
            word = '무료' if self.mode == 'free' else '없음'
            return CostTerm(cost_type=self.kind, source_text=source, amount=word, included_in_price=False,
                            evidence=evidence)
        if self.mode == 'per_order':
            return CostTerm(cost_type=self.kind, source_text=source, amount=self.amount, basis='회당',
                            basis_type='PER_ORDER', included_in_price=False, evidence=evidence)
        if self.mode == 'per_box':
            return CostTerm(cost_type=self.kind, source_text=source, amount=self.amount, basis='박스당',
                            basis_type='PER_UNIT', basis_unit='box', included_in_price=False, evidence=evidence)
        if self.mode == 'unpriced':
            return CostTerm(cost_type=self.kind, source_text=source, amount=None, included_in_price=False,
                            evidence=evidence)
        raise ValueError(self.mode)


TAX_TEXT = {'EXCLUDED': '부가세 별도', 'INCLUDED': '부가세 포함', 'EXEMPT': '부가세 면세'}


@dataclass
class Quote:
    """업체 한 곳의 견적 메일. 값을 None으로 두면 메일에도 견적에도 없다.
    omit에 이름을 넣으면 메일 원문에는 있지만 견적에는 저장하지 않는다(재추출 시나리오: 'cost:SHIPPING'·'tax_terms' 등)."""
    supplier: str
    role: str = 'REPLY'               # INITIAL(기존 업체 인상 통보) / REPLY
    price: str | None = '5,250원'
    unit: str | None = 'kg'
    available: str | None = '2,000kg'
    available_text: str | None = None # 공급 가능량 문장을 직접 정할 때 ('월 2,000kg 공급 가능')
    previous: str | None = None       # 인상 전 단가 ('5,000원')
    previous_text: str | None = None  # 인상 문장을 직접 정할 때
    reason: str | None = None         # 인상 사유
    costs: list[Cost] = field(default_factory=lambda: [Cost()])
    tax: str | None = 'EXCLUDED'      # EXCLUDED·INCLUDED·EXEMPT 또는 None(언급 없음)
    tax_text: str | None = None       # 포함·별도를 알 수 없는 세금 문구 ('세금계산서 발행')
    spec: str | None = SPEC
    cert: str | None = 'HACCP'
    moq: str | None = None            # '700kg'
    multiple: str | None = None       # '300kg'
    per_box: str | None = None        # 박스당 중량 ('10kg'). unit이 박스일 때
    delivery: str | None = None       # '발주 후 3일 내 납품'
    notes: list[tuple[str, str]] = field(default_factory=list)   # [(문장, 분류)] 품목 비고
    extra: list[str] = field(default_factory=list)               # 견적에 저장하지 않는 메일 문장 (지시문 등)
    omit: set[str] = field(default_factory=set)
    attachment: list[str] | None = None   # 본문 대신 첨부 1쪽에 둘 문장들 (본문은 "첨부 참고")

    @property
    def mail_id(self) -> str:
        return f'm-{self.supplier}'


def _quote_lines(q: Quote, name: str) -> list[tuple[str, list[QuoteField], list[CostTerm], list[ItemNote]]]:
    """메일 문장과 그 문장에서 저장할 값. 값의 raw_value는 문장 안의 표기 그대로다."""
    e = EvidenceRef(mail_id=q.mail_id, part='att-1', page=1) if q.attachment is not None else EvidenceRef(mail_id=q.mail_id)
    f = lambda name_, raw, **kw: QuoteField(field_name=name_, raw_value=raw, evidence=e, **kw)
    lines = [(f'{name}입니다.', [f('supplier_name', name)], [], [])]
    if q.price is not None:
        unit_word = {'kg': 'kg', '박스': '박스'}.get(q.unit or '', q.unit or '')
        if q.role == 'INITIAL' and q.previous_text:
            text = q.previous_text
        elif q.role == 'INITIAL' and q.previous:
            text = f'{ITEM} 단가를 기존 {q.previous}/{unit_word}에서 {q.price}/{unit_word}으로 인상 요청드립니다.'
        elif q.unit is None:
            text = f'이번 {ITEM} 공급 단가는 {q.price}입니다.'
        else:
            text = f'이번 {ITEM} 공급 단가는 {q.price}/{unit_word}입니다.'
        fields = [f('unit_price', q.price)]
        if ITEM in text:
            fields.insert(0, f('item_name', ITEM))
        else:
            lines.append((f'품목은 {ITEM}입니다.', [f('item_name', ITEM)], [], []))
        if q.unit is not None:
            fields.append(f('unit', unit_word, **({'unit_code': 'box'} if q.unit == '박스' else {})))
        if q.role == 'INITIAL' and q.previous and 'previous_unit_price' not in q.omit:
            fields.append(f('previous_unit_price', q.previous))
        lines.append((text, fields, [], []))
    if q.reason:
        lines.append((f'인상 사유는 {q.reason}입니다.', [f('increase_reason', q.reason)], [], []))
    spec_cert = []
    if q.spec is not None:
        spec_cert.append((f'규격은 {q.spec}입니다.', 'specification', q.spec))
    if q.cert is not None:
        spec_cert.append((f'인증은 {q.cert}입니다.', 'certifications', q.cert))
    for text, name_, raw in spec_cert:
        lines.append((text, [] if name_ in q.omit else [f(name_, raw)], [], []))
    if q.per_box is not None:
        lines.append((f'박스당 {q.per_box}입니다.',
                      [] if 'quantity_per_order_unit' in q.omit else [f('quantity_per_order_unit', q.per_box)], [], []))
    if q.available is not None:
        text = q.available_text or (f'이번 공급 계획 물량은 {q.available}입니다.' if q.role == 'INITIAL'
                                    else f'이번 공급 가능량은 최대 {q.available}입니다.')
        lines.append((text, [] if 'available_quantity' in q.omit else [f('available_quantity', q.available)], [], []))
    if q.moq is not None:
        lines.append((f'최소 주문량은 {q.moq}입니다.', [] if 'moq' in q.omit else [f('moq', q.moq)], [], []))
    if q.multiple is not None:
        lines.append((f'주문은 {q.multiple} 단위로 받습니다.', [] if 'pack_multiple' in q.omit else [f('pack_multiple', q.multiple)],
                      [], []))
    if q.delivery is not None:
        lines.append((f'{q.delivery}입니다.', [] if 'delivery_date' in q.omit else [f('lead_time', q.delivery)], [], []))
    for cost in q.costs:
        phrase = cost.phrase()
        omitted = f'cost:{cost.kind}' in q.omit
        lines.append((f'{phrase}입니다.' if not phrase.endswith('다') else f'{phrase}.', [], [] if omitted else [cost.term(q.mail_id)], []))
    if q.tax is not None or q.tax_text is not None:
        raw = q.tax_text or TAX_TEXT[q.tax]
        kw = {'tax_basis': q.tax} if q.tax and not q.tax_text else {}
        lines.append((f'{raw}입니다.', [] if 'tax_terms' in q.omit else [f('tax_terms', raw, **kw)], [], []))
    for text, category in q.notes:
        lines.append((text, [], [], [ItemNote(text=text, category=category, evidence=e)]))
    for text in q.extra:
        lines.append((text, [], [], []))
    # 같은 원문 구절 안의 비용은 이 메일 근거를 쓴다 (첨부 메일이면 첨부)
    if q.attachment is not None:
        for _, _, costs, _ in lines:
            for cost in costs:
                cost.evidence = e
    return lines


def quote_mail(q: Quote, name: str, order: int) -> tuple[dict, list[QuoteField], list[CostTerm], list[ItemNote]]:
    lines = _quote_lines(q, name)
    text = '\n'.join(line for line, *_ in lines)
    fields = [field_ for _, fs, _, _ in lines for field_ in fs]
    costs = [cost for _, _, cs, _ in lines for cost in cs]
    notes = [note for _, _, _, ns in lines for note in ns]
    mail = {'mail_id': q.mail_id, 'channel': 'MAIL', 'message_role': 'INITIAL' if q.role == 'INITIAL' else 'REPLY',
            'direction': 'INBOUND', 'supplier_id': f's-{q.supplier}', 'received_at': f'2026-11-{10 + order:02d}T09:00:00+09:00',
            'subject': f'[{name}] {ITEM} ' + ('단가 인상 안내' if q.role == 'INITIAL' else '견적 회신'),
            'body': text, 'attachments': []}
    if q.attachment is not None:
        mail['body'] = f'{name}입니다. 견적 내용은 첨부 참고 부탁드립니다.'
        mail['attachments'] = [{'attachment_ref': 'att-1', 'filename': '견적서.pdf', 'pages': 1, 'status': 'READ',
                                'texts': ['\n'.join([text, *q.attachment])]}]
        # 공급자명 근거는 본문으로 둔다
        for field_ in fields:
            if field_.field_name == 'supplier_name':
                field_.evidence = EvidenceRef(mail_id=q.mail_id)
    return mail, fields, costs, notes


# 세계 만들기

DEFAULT_CASE = {'item_name': ITEM, 'specification': SPEC, 'mandatory_conditions': ['HACCP'], 'quantity': '1000',
                'unit': 'kg', 'due_date': '2026-12-01', 'policy': {}}


@dataclass
class World:
    suppliers: list[Supplier]
    quotes: list[Quote] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    case: dict = field(default_factory=dict)
    style: dict = field(default_factory=lambda: {'company_name': '견줘푸드', 'signature': '견줘푸드 구매팀'})
    after_seed: dict = field(default_factory=dict)     # 견적 저장 뒤 덮어쓸 업무 값 (예: quantity None)
    revision_changed: bool = False                     # 실행 중 업무가 바뀐 상황: 에이전트에는 이전 revision을 준다

    def supplier(self, key: str) -> Supplier:
        return next(s for s in self.suppliers if s.key == key)

    def build(self) -> MemoryStore:
        case = copy.deepcopy(DEFAULT_CASE) | copy.deepcopy(self.case)
        suppliers = [{'id': s.id, 'name': s.name or s.key, 'contact_email': f'{s.key}@example.com' if s.email else None,
                      'evidence_jsonb': {'source': 'web'} if s.web else {}, 'source_type': 'VERIFIED_WEB' if s.web else 'REGISTERED'}
                     for s in self.suppliers]
        items, purchases = [], []
        for item in self.items:
            items.append({'id': item.id, 'supplier_id': f's-{item.supplier}', 'item_name': item.item_name,
                          'specification': item.specification, 'unit': 'kg', 'certifications_jsonb': item.certifications,
                          'origin': item.origin, 'notes': item.notes, 'last_purchase_id': None,
                          'standardized_item_key': None})
            for index, (day, price) in enumerate(item.history):
                purchases.append({'supplier_item_id': item.id, 'supplier_id': f's-{item.supplier}',
                                  'transaction_date': day, 'quantity': '1000', 'unit': 'kg', 'unit_price': str(price),
                                  'total_amount': str(price * 1000), 'currency': 'KRW'})
        store = MemoryStore(case, suppliers, items, purchases, [], self.style)
        context = RunContext(CASE_ID, store.case['input_revision'])
        for order, quote in enumerate(self.quotes):
            name = self.supplier(quote.supplier).name or quote.supplier
            mail, fields, costs, notes = quote_mail(quote, name, order)
            store.mails[mail['mail_id']] = mail
            result = save_structured_quote(store, context, fields, [], costs, notes)
            if result.get('status') != 'STORED':
                raise ValueError(f'견적 저장 실패 {quote.supplier}: {result}')
        store.case.update(copy.deepcopy(self.after_seed))
        store.calls.clear()
        return store

    def quote_id(self, store: MemoryStore, supplier_key: str) -> str:
        found = store.latest_quote_version(CASE_ID, f's-{supplier_key}')
        if found is None:
            raise KeyError(supplier_key)
        return found[0]

    def agent_revision(self, store: MemoryStore) -> int:
        """에이전트 입력에 넣을 revision. 업무가 바뀐 상황이면 저장소 쪽을 하나 올린다."""
        revision = store.case['input_revision']
        if self.revision_changed:
            store.case['input_revision'] = revision + 1
        return revision
