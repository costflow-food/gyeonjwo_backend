"""Team records -> existing calculation contract; no synthetic data or defaults.

SupplementStore and ValidationStore are private, unresolved backend ports. They
are deliberately NOT additions to CaseStore/QuoteStore. Missing ports/facts hold.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Protocol

from app.core.errors import ReviewRequired
from app.features.review.domain.validation import decimal_value, explicit_date, unit_name
from app.agents.kan60.counter_offer.contract import digest
from .repository import BridgeError, Scope


class MissingTeamData(BridgeError):
    def __init__(self, fields=(), *, issues=()):
        self.issues = [*issues, *({'action': 'HOLD', 'field': f} for f in fields)]
        self.action = next(a for a in ('HOLD', 'RECHECK_SOURCE', 'NEEDS_EVIDENCE')
                           if any(i['action'] == a for i in self.issues))
        self.fields = sorted({i['field'] for i in self.issues if i['action'] == self.action})
        super().__init__(next((i['reason'] for i in self.issues if i['action'] == self.action and 'reason' in i), 'TEAM_DATA_REQUIRED'))


class SupplementStore(Protocol):
    """Only facts absent from current team fields, with scoped source evidence.

    load(scope) -> {case_id, input_revision, quote_version_ids, team_record_digest, facts}.
    facts[path] = {value, evidence: {mail_id, part, page, excerpt}}.
    No complete engine input, quote price, policy, quantity or default values.
    This port needs a backend/contract owner; tests use a storage double.
    """
    def load(self, scope: Scope) -> dict | None: ...


class ValidationStore(Protocol):
    """Backend must atomically check scope/current source digest on writes.

    resolve must verify kind, case, revision, exact quote IDs, source digest and
    payload integrity. An agent run ID is write provenance, not a read restriction.
    Team CaseStore.save_agent_output is NOT this persistence contract.
    """
    def put_if_current(self, scope, source_digest, kind, payload, agent_run_id) -> str: ...
    def resolve(self, scope, source_digest, kind, ref) -> dict: ...


def team_record_digest(case, rows):
    """Bind reviewed supplemental facts to the exact team case and quote records."""
    return digest({'case': case, 'quotes': {qid: row.model_dump(mode='json') for qid, row in rows.items()}})


def _number(value, path):
    if value is None:
        raise MissingTeamData([path])
    if not isinstance(value, str):
        raise BridgeError('TEAM_NUMERIC_VALIDATION_FAILED:' + path)
    try:
        return format(decimal_value(value, path), 'f')
    except ReviewRequired as exc:
        raise BridgeError('TEAM_NUMERIC_VALIDATION_FAILED:' + path) from exc


def _verify_number_source(name, entry):
    # Reuse ①'s existing normalization, including MOQ and price-unit syntax.
    # Lazy import avoids a registration-time tools -> product -> tools cycle.
    from app.agents.tools import QUOTE_FIELDS, _parse
    kind = QUOTE_FIELDS[name][0]
    if kind not in ('price', 'number'):
        return
    try:
        normalized = _parse(kind, name, entry['raw_value'])
        if decimal_value(normalized, name) != decimal_value(entry['value'], name):
            raise BridgeError('REFERENCE_SOURCE_NUMERIC_MISMATCH:' + name)
    except (ReviewRequired, TypeError, KeyError, AttributeError) as exc:
        raise BridgeError('REFERENCE_SOURCE_NUMERIC_INVALID:' + name) from exc


# Numeric evidence needs its field's clause, not an unrelated equal number in
# the same mail. Unknown/ambiguous labels require source re-extraction.
_NUMERIC_SOURCE_LABELS = {
    'unit_price': ('unit_price', '요청 단가', '인상 단가', '변경 단가', '단가'),
    'previous_unit_price': ('previous_unit_price', '기존 단가', '이전 단가', '인상 전 단가'),
    'available_quantity': ('available_quantity', '공급 가능량', '공급 가능 수량', '공급량'),
    'moq': ('moq', 'MOQ', '최소 주문 수량', '최소 수량', '최소'),
    'pack_multiple': ('pack_multiple', '포장 배수', '주문 배수', '주문 단위', '포장 단위'),
}


def _source_contains(text, raw):
    """Normalize whitespace/thousands separators, retaining numeric boundaries.

    Never concatenate lines: 55 on one line and 00 on the next is not 5500.
    Also reject numeric substrings embedded after signs/decimal punctuation.
    """
    from app.agents.tools import _normalized
    raw = _normalized(raw, True)
    pattern = re.escape(raw)
    if raw[:1].isdigit():pattern = r'(?<![\d.,+\-])' + pattern
    if raw[-1:].isdigit():pattern += r'(?![\d.]|,\d)'
    return any(re.search(pattern, _normalized(line, True)) is not None for line in text.splitlines())


def _numeric_field_source(text, raw, field):
    from app.agents.tools import _normalized, _parse, QUOTE_FIELDS
    expected = decimal_value(_parse(QUOTE_FIELDS[field][0], field, raw), field)
    labels = {label.replace(' ', ''): key for key, names in _NUMERIC_SOURCE_LABELS.items() for label in names}
    pattern = r'(?<![A-Za-z_])(?:' + '|'.join(re.escape(k) for k in sorted(labels, key=len, reverse=True)) + r')(?![A-Za-z_])'
    matched = []
    for line in text.splitlines():
        compact = ''.join(line.split())
        anchors = list(re.finditer(pattern, compact))
        for i, anchor in enumerate(anchors):
            end = anchors[i+1].start() if i+1 < len(anchors) else len(compact)
            # A different labelled column/clause cannot lend its number.
            clause = re.split(r'[|;\t]|[A-Za-z_]+:', compact[anchor.end():end])[0]
            if labels[anchor.group()] == field:
                number = re.search(r'[-+]?\d+(?:\.\d+)?', _normalized(clause, True))
                matched.append(bool(number) and number.group()[0].isdigit() and decimal_value(number.group(), field) == expected and
                               _source_contains(anchor.group() + clause, raw))
        if not anchors:
            # Team extraction accepts the explicit quantity phrase itself.
            if field == 'pack_multiple' and re.search(r'(?:단위|배수)', raw) and _source_contains(line, raw):
                matched.append(True)
    return matched == [True]


@dataclass(frozen=True)
class TeamReviewRepository:
    cases: object
    quotes: object
    supplements: SupplementStore | None = None
    references: ValidationStore | None = None
    context: object = None

    def for_run(self, context):
        return replace(self, context=context)

    def current_revision(self, case_id):
        if self.cases is None or self.quotes is None:
            raise MissingTeamData(['CaseStore', 'QuoteStore'])
        value = self.cases.current_input_revision(case_id)
        if value is None:
            raise BridgeError('CASE_SCOPE_DENIED')
        return value

    def scope_for_case(self, case_id, revision):
        case = self.cases.get_work_case(case_id)
        if case is None:
            raise BridgeError('CASE_SCOPE_DENIED')
        scope = Scope(case_id, revision, tuple(sorted(case['latest_quote_version_ids'])))
        self.snapshot(scope)
        return scope

    def snapshot(self, scope):
        if self.current_revision(scope.case_id) != scope.revision:
            raise BridgeError('STALE_REVISION')
        if self.quotes.current_input_revision(scope.case_id) != scope.revision:
            raise BridgeError('STALE_REVISION')
        if self.context is not None and (self.context.case_id != scope.case_id or
                                         self.context.input_revision != scope.revision):
            raise BridgeError('CASE_SCOPE_RUN_CONTEXT_MISMATCH')
        case = self.cases.get_work_case(scope.case_id)
        if case is None:
            raise BridgeError('CASE_SCOPE_DENIED')
        if case.get('input_revision') != scope.revision:
            raise BridgeError('STALE_REVISION')
        ids = case.get('latest_quote_version_ids')
        if not ids or len(ids) != len(set(ids)) or tuple(sorted(ids)) != scope.quote_ids:
            raise BridgeError('STALE_QUOTE_VERSIONS')
        mails = {m['mail_id']: m for m in self.cases.list_case_mails(scope.case_id)}
        # The existing interface only supports lookup by supplier. Discover known
        # suppliers from scoped mails, then insist the full latest-ID set resolved.
        rows = {}
        for supplier in sorted({m['supplier_id'] for m in mails.values() if m.get('supplier_id')}):
            found = self.quotes.latest_quote_version(scope.case_id, supplier)
            if found is not None:
                qid, row = found
                if row.work_case_id != scope.case_id or row.supplier_id != supplier:
                    raise BridgeError('CASE_SCOPE_QUOTE_MISMATCH')
                if qid in ids:
                    if qid in rows:
                        raise BridgeError('REFERENCE_SCOPE_DUPLICATE_QUOTE')
                    rows[qid] = row
        if set(rows) != set(ids):
            raise MissingTeamData(['QuoteStore.get_quote_version(case_id, quote_version_id)'])
        mapping = _Mapper(self.cases, scope, case, mails, rows, self.supplements)
        snapshot = mapping.build()
        # Catch changes during reads, including quote replacement without revision.
        if self.current_revision(scope.case_id) != scope.revision:
            raise BridgeError('STALE_REVISION')
        latest = self.cases.get_work_case(scope.case_id)
        if latest is None or tuple(sorted(latest['latest_quote_version_ids'])) != scope.quote_ids:
            raise BridgeError('STALE_QUOTE_VERSIONS')
        return snapshot

    def put_if_current(self, scope, kind, payload):
        snapshot = self.snapshot(scope)
        if self.references is None:
            raise MissingTeamData(['ValidationStore.put_if_current'])
        if self.context is None or not self.context.agent_run_id:
            raise MissingTeamData(['RunContext.agent_run_id'])
        return self.references.put_if_current(scope, digest(snapshot), kind, deepcopy(payload),
                                              self.context.agent_run_id)

    def resolve(self, scope, kind, ref):
        snapshot = self.snapshot(scope)
        if self.references is None:
            raise MissingTeamData(['ValidationStore.resolve'])
        return self.references.resolve(scope, digest(snapshot), kind, ref)


class _Mapper:
    def __init__(self, store, scope, case, mails, rows, supplements):
        self.store, self.scope, self.case, self.mails, self.rows = store, scope, case, mails, rows
        self.evidence, self.missing, self.issues = {}, [], []
        extra = supplements.load(scope) if supplements is not None else None
        self.facts = {}
        if extra is not None:
            if (extra.get('case_id') != scope.case_id or extra.get('input_revision') != scope.revision or
                    sorted(extra.get('quote_version_ids', [])) != list(scope.quote_ids)):
                raise BridgeError('REFERENCE_SCOPE_SUPPLEMENT_MISMATCH')
            if extra.get('team_record_digest') != team_record_digest(case, rows):
                raise BridgeError('REFERENCE_SOURCE_SUPPLEMENT_CHANGED')
            self.facts = extra['facts']

    def need(self, value, path):
        if value is None or value == '':
            self.missing.append(path)
        return deepcopy(value)

    def issue(self, action, field, *, stop=False):
        from app.agents.tools import EXTRACTION_FIELD_NAMES
        if action == 'RECHECK_SOURCE' and field not in EXTRACTION_FIELD_NAMES:
            raise ValueError('Unsupported extraction field')
        self.issues.append({'action': action, 'field': field})
        if stop:
            raise MissingTeamData(self.missing, issues=self.issues)

    def extraction_source(self, field, load):
        try:
            return load()
        except MissingTeamData as exc:
            if exc.fields == ['evidence.excerpt']:
                self.issue('RECHECK_SOURCE', field, stop=True)
            raise
        except BridgeError as exc:
            if exc.code.startswith(('REFERENCE_SOURCE_NUMERIC', 'REFERENCE_SOURCE_FIELD',
                                    'REFERENCE_SOURCE_COST', 'REFERENCE_SOURCE_SHIPPING')) or exc.code == 'REFERENCE_SOURCE_EVIDENCE_MISMATCH':
                self.issue('RECHECK_SOURCE', field, stop=True)
            raise

    def source(self, entry, supplier=None, field=None):
        """Check stored original evidence against scoped current source text.

        ① stores location, not part/page in evidence_jsonb. Resolve that exact
        location through the existing mail/attachment interface, never guess body.
        """
        mail_id = entry.get('mail_id')
        mail = self.mails.get(mail_id)
        if mail is None or supplier is not None and mail.get('supplier_id') != supplier:
            raise BridgeError('CASE_SCOPE_EVIDENCE_MISMATCH')
        raw = entry.get('excerpt', entry.get('raw_value', entry.get('source_text')))
        if not isinstance(raw, str) or not raw.strip():
            raise MissingTeamData(['evidence.excerpt'])
        if 'part' in entry:
            candidates = [(entry['part'], entry.get('page'))]
        else:
            candidates = [('body', None)] + [(a['attachment_ref'], p)
                for a in mail.get('attachments', []) for p in range(1, a['pages'] + 1)]
        matches = []
        for part, page in candidates:
            found = self.store.read_mail_part(self.scope.case_id, mail_id, part, page)
            if found is None:
                continue
            location = found.get('location')
            if 'location' in entry and (not location or not
                    (entry['location'] == location or entry['location'].startswith(location + ' '))):
                continue
            if (_source_contains(found['text'], raw) and
                    (field not in _NUMERIC_SOURCE_LABELS or _numeric_field_source(found['text'], raw, field))):
                matches.append({'mail_id': mail_id, 'part': part, 'page': page,
                                'location': location, 'text': found['text'], 'stored_evidence': deepcopy(entry)})
        if len(matches) != 1:
            raise BridgeError('REFERENCE_SOURCE_EVIDENCE_MISMATCH')
        ref = 'team-source:' + digest(matches[0])
        self.evidence[ref] = matches[0]
        return ref

    def fact(self, path, *, required=True, evidence=True):
        entry = self.facts.get(path)
        if entry is None or entry.get('value') is None:
            if required:
                self.missing.append('review_facts.' + path)
            return None
        if evidence:
            self.source(entry['evidence'])
        return deepcopy(entry['value'])

    def period_cost(self, qid, row, kind):
        from app.agents.tools import _contains, _normalized, _inclusion, _ZERO_AMOUNTS
        costs = [c for c in row.cost_terms_jsonb if c.get('cost_type') == kind]
        if len(costs) != 1:
            self.issue('NEEDS_EVIDENCE' if not costs else 'RECHECK_SOURCE', 'cost_terms')
            return None, None
        cost = costs[0]
        ref = self.source(cost, row.supplier_id)
        text = cost['source_text']
        if cost.get('included_in_price') is not None and _inclusion(text) != cost['included_in_price']:
            raise BridgeError('REFERENCE_SOURCE_COST_INCLUSION_MISMATCH')
        if cost.get('included_in_price') is True:
            return '0', ref  # explicit inclusion, never inferred from absent terms
        if (cost.get('basis') in ('PER_SUPPLIER_PER_PERIOD', '비교기간 전체') and
                cost.get('included_in_price') is False and cost.get('payer') in ('BUYER', '구매사')):
            if any(not _contains(_normalized(text, False), _normalized(cost[key], False), False)
                   for key in ('basis', 'payer')):
                raise BridgeError('REFERENCE_SOURCE_COST_BASIS_MISMATCH')
            amount = cost.get('amount')
            if amount is not None and not (_contains(_normalized(text, True), _normalized(amount, True), True) or
                    decimal_value(amount, 'cost.amount') == 0 and any(word in text for word in _ZERO_AMOUNTS)):
                raise BridgeError('REFERENCE_SOURCE_COST_AMOUNT_MISMATCH')
            if kind == 'SHIPPING' and row.shipping_cost is not None and row.shipping_cost != amount:
                raise BridgeError('REFERENCE_SOURCE_SHIPPING_COLUMN_MISMATCH')
            if cost.get('amount') is None:
                self.issue('NEEDS_EVIDENCE', 'cost_terms')
            return cost.get('amount'), ref
        if any(cost.get(key) is None for key in ('basis', 'payer', 'included_in_price')):
            self.issue('NEEDS_EVIDENCE', 'cost_terms')
        else:
            self.missing.append('UNSUPPORTED_COST_MAPPING:' + qid + '.' + kind)
        return None, ref

    def field(self, qid, row, name, value):
        entry = row.evidence_jsonb.get(name)
        if value is None:
            self.issue('RECHECK_SOURCE' if entry and entry.get('raw_value') else 'NEEDS_EVIDENCE', name)
            return None
        if entry is None:
            self.issue('RECHECK_SOURCE', name)
            return value
        if entry.get('value') != value:
            self.issue('RECHECK_SOURCE', name, stop=True)
        self.extraction_source(name, lambda: _verify_number_source(name, entry))
        self.extraction_source(name, lambda: self.source(entry, row.supplier_id, name))
        return deepcopy(value)

    def pack_multiple(self, row, qid):
        pricing = row.pricing_terms_jsonb
        if pricing.get('pack_multiple') is None:
            if row.evidence_jsonb.get('pack_multiple', {}).get('raw_value'):
                self.issue('RECHECK_SOURCE', 'pack_multiple', stop=True)
            return {}
        value = self.field(qid, row, 'pack_multiple', pricing['pack_multiple'])
        entry = row.evidence_jsonb.get('pack_multiple')
        if entry is None:
            raise MissingTeamData(self.missing, issues=self.issues)
        from app.agents.tools import _quantity
        _, raw_unit = _quantity(entry['raw_value'], 'pack_multiple')
        units = [u for u in (raw_unit, entry.get('quantity_unit'), pricing.get('pack_multiple_unit')) if u is not None]
        if not units:
            raise MissingTeamData(issues=[{'action':'NEEDS_EVIDENCE', 'field':'pack_multiple', 'reason':'PACK_MULTIPLE_UNIT_REQUIRED'}])
        if len({unit_name(u) for u in units}) != 1:
            self.issue('RECHECK_SOURCE', 'pack_multiple', stop=True)
        unit = unit_name(units[0])
        if unit != unit_name(row.unit):
            raise BridgeError('PACK_MULTIPLE_UNIT_UNSUPPORTED:' + qid)
        if decimal_value(value, 'pack_multiple') <= 0:
            self.issue('RECHECK_SOURCE', 'pack_multiple', stop=True)
        ref = self.extraction_source('pack_multiple', lambda: self.source(entry, row.supplier_id, 'pack_multiple'))
        return {'pack_multiple': _number(value, 'pack_multiple'), 'pack_multiple_unit': unit,
                'pack_multiple_source_ref': ref}

    def price_tiers(self, qid, row):
        """Read ①'s unchanged record shape; meaning must be explicit in its source.

        No inference from price monotonicity or from missing endpoint operators.
        A table-wide rule is accepted only in each tier's own source part.
        """
        from app.agents.tools import _quantity, _normalized
        from app.agents.kan60.purchase_plans.contract import (ReviewError, n,
            validate_price_tiers, validate_tier_coverage)

        def reject(reason, action='NEEDS_EVIDENCE'):
            raise MissingTeamData(self.missing, issues=[*self.issues,
                {'action': action, 'field': 'price_tiers' if action != 'HOLD' else reason, 'reason': reason}])

        records = row.pricing_terms_jsonb['price_tiers']
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            reject('TIER_TABLE_INVALID', 'RECHECK_SOURCE')
        all_units = {'주문총수량에해당하는단가를전량에적용합니다',
                     '주문전체수량에해당하는구간단가를전량에적용합니다',
                     '수량구간은주문총수량기준이며해당단가를전량에적용합니다'}
        graduated = {'각구간에속한물량에각구간단가를적용합니다',
                     '구간별물량에각각단가를적용하는누진방식입니다'}
        canonical, method_refs = [], []
        for record in records:
            endpoints, raw_bounds = {}, []
            for side, operators in [('min', {'이상': True, '초과': False, '부터': True}),
                                    ('max', {'이하': True, '미만': False, '까지': True})]:
                value, raw = record.get(side + '_quantity'), record.get(side + '_quantity_raw')
                endpoints[side + '_quantity'] = value
                endpoints[side + '_inclusive'] = False
                if value is None:
                    if raw is not None:reject('TIER_BOUNDARY_VALUE_MISMATCH', 'RECHECK_SOURCE')
                    continue
                if not isinstance(raw, str):reject('TIER_BOUNDARY_EVIDENCE_MISSING', 'RECHECK_SOURCE')
                op = next((op for op in operators if raw.strip().endswith(op)), None)
                if op is None:reject('TIER_BOUNDARY_AMBIGUOUS')
                try:amount, unit = _quantity(raw, side + '_quantity')
                except ReviewRequired:reject('TIER_BOUNDARY_VALUE_MISMATCH', 'RECHECK_SOURCE')
                if decimal_value(amount, 'tier.quantity') != decimal_value(value, 'tier.quantity'):
                    reject('TIER_BOUNDARY_VALUE_MISMATCH', 'RECHECK_SOURCE')
                if unit is None:reject('TIER_QUANTITY_UNIT_REQUIRED')
                if unit_name(unit) != unit_name(row.unit):reject('TIER_QUANTITY_UNIT_MISMATCH', 'HOLD')
                endpoints[side + '_inclusive'] = operators[op]
                raw_bounds.append(raw)
            if not raw_bounds:reject('TIER_BOUNDARY_AMBIGUOUS')
            ref = self.extraction_source('price_tiers', lambda: self.source(
                {**record, 'excerpt': raw_bounds[0]}, row.supplier_id))
            source = self.evidence[ref]
            lines = [_normalized(line, True) for line in source['text'].splitlines() if line.strip()]
            price = _number(record.get('unit_price'), 'price_tiers.unit_price')
            matches = []
            for line in lines:
                if not all(_source_contains(line, raw) for raw in raw_bounds):continue
                amounts = re.findall(r'(?<![\d.])(\d+(?:\.\d+)?)(?:원|KRW)(?:/([A-Za-z가-힣]+))?', line)
                if len(amounts) == 1 and any(decimal_value(v, 'tier.unit_price') == decimal_value(price, 'tier.unit_price') and
                       (not u or unit_name(u) == unit_name(row.unit)) for v, u in amounts):
                    matches.append(line)
            if len(matches) != 1:reject('TIER_ROW_EVIDENCE_MISMATCH', 'RECHECK_SOURCE')
            statements = {''.join(line.split()).rstrip('.。').removeprefix('단가적용방식:')
                          for line in source['text'].splitlines()}
            is_all, is_graduated = bool(statements & all_units), bool(statements & graduated)
            if is_all and (is_graduated or '누진' in source['text']):reject('TIER_METHOD_AMBIGUOUS')
            if is_graduated:reject('TIER_GRADUATED_UNSUPPORTED', 'HOLD')
            if not is_all:reject('TIER_METHOD_AMBIGUOUS')
            method_refs.append(ref)
            canonical.append({**endpoints, 'unit_price': price, 'source_ref': ref})
        canonical.sort(key=lambda t: (t['min_quantity'] is not None,
                                     decimal_value(t['min_quantity'], 'tier.quantity') if t['min_quantity'] is not None else 0))
        try:
            validate_price_tiers(canonical)
            if row.available_quantity is not None and row.pricing_terms_jsonb.get('moq') is not None:
                validate_tier_coverage(canonical, n(row.pricing_terms_jsonb['moq']),
                    min(n(row.available_quantity), n(_number(self.case.get('quantity'), 'quantity'))))
        except ReviewError as exc:reject(exc.issue['code'])
        method_ref = 'team-tier-method:' + digest(method_refs)
        self.evidence[method_ref] = {'pricing_method': 'ALL_UNITS', 'source_refs': method_refs}
        return canonical, method_ref

    def build(self):
        case, scope = self.case, self.scope
        policy = self.need(case.get('policy'), 'policy')
        # Policy JSON shape is not specified by CaseStore. Accept only the explicit
        # existing calculation policy, validated again by parse(); no translations
        # of unknown policy keys or implicit approval/limits.
        if isinstance(policy, dict) and policy.get('source_ref'):
            self.evidence[policy['source_ref']] = {'case_id': scope.case_id, 'input_revision': scope.revision,
                                                 'field': 'policy', 'value': deepcopy(policy)}
        # Evaluation date is execution metadata, not a fact to extract from mail.
        as_of, period = self.fact('as_of', evidence=False), self.fact('period')
        if period is not None and case.get('due_date') is not None:
            if explicit_date(period['to'], 'period.to') > explicit_date(case['due_date'], 'due_date'):
                raise BridgeError('TEAM_REQUEST_DUE_DATE_MISMATCH')
        identity = self.fact('item_identity', required=False)
        if identity is not None:
            identity.pop('item_id', None)  # opaque calculator ID is not a comparison condition
        item = {**(identity or {}), 'name': self.need(case.get('item_name'), 'item_name'),
                'specification': self.need(case.get('specification'), 'specification')}
        terms = self.fact('terms')
        candidates = {row.supplier_id for row in self.rows.values() if any(
            'previous_unit_price' in m.get('fields', {}) for m in row.mail_normalized.values())}
        incumbent_id = self.fact('incumbent_supplier_id', required=len(candidates) != 1)
        if incumbent_id is None and len(candidates) == 1:
            incumbent_id = next(iter(candidates))
        elif len(candidates) == 1 and incumbent_id not in candidates:
            raise BridgeError('REFERENCE_SCOPE_INCUMBENT_MISMATCH')
        fixed = self.fact('fixed_commitment')
        if fixed is not None:
            ref = self.source(self.facts['fixed_commitment']['evidence'])
            fixed = {**fixed, 'source_ref': ref}
        split_possible = not isinstance(policy, dict) or not (policy.get('allow_split') is False or policy.get('max_suppliers') == 1)
        repricing = self.fact('repricing_scope', required=split_possible)
        quote_data, bindings, notice = [], {}, None
        for qid, row in sorted(self.rows.items()):
            if row.source_mail_id not in self.mails or self.mails[row.source_mail_id].get('supplier_id') != row.supplier_id:
                raise BridgeError('CASE_SCOPE_QUOTE_MAIL_MISMATCH')
            if row.extraction_status != 'CONFIRMED':
                self.missing.append(qid + '.extraction_status.CONFIRMED')
            def field(name, value):
                return self.field(qid, row, name, value)
            def checked(name):
                check = row.checks_jsonb.get(name, {})
                if check.get('value') is not None and check.get('state') != 'STATED':
                    self.issue('RECHECK_SOURCE', name)
                return field(name, check.get('value'))
            pricing = row.pricing_terms_jsonb
            qi = self.fact(qid + '.item_identity', required=False) or {}
            qi.pop('item_id', None)
            qi.update(name=checked('item_name'), specification=checked('specification'))
            if row.checks_jsonb.get('origin', {}).get('value') is not None or 'origin' in item:
                qi['origin'] = checked('origin')
            for condition in ('grade', 'processing'):
                if condition in item and condition not in qi:
                    self.missing.append('review_facts.' + qid + '.item_identity.' + condition)
            unit = field('unit', row.unit)
            for key in ('moq', 'available_quantity'):
                if pricing.get(key + '_unit', unit) != unit:
                    raise BridgeError('TEAM_QUANTITY_UNIT_MISMATCH:' + qid + '.' + key)
            if pricing.get('order_unit', unit) != unit or pricing.get('quantity_per_order_unit') not in (None, '1'):
                raise BridgeError('TEAM_ORDER_UNIT_CONVERSION_UNSUPPORTED')
            tiers, tier_method_ref = self.price_tiers(qid, row) if pricing.get('price_tiers') else (None, None)
            if tiers and row.unit_price is not None:
                raise MissingTeamData(issues=[{'action': 'RECHECK_SOURCE', 'field': 'price_tiers', 'reason': 'TIER_FLAT_PRICE_CONFLICT'}])
            price = None if tiers else field('unit_price', row.unit_price)
            effective = field('effective_from', row.effective_from) if row.supplier_id == incumbent_id or row.effective_from is not None else None
            until = field('valid_until', row.valid_until)
            currency = field('currency', pricing.get('currency'))
            qt = self.fact(qid + '.terms')
            tax_raw = checked('tax_terms')
            # Conservative exact labels only; never infer tax from a bare price.
            tax = {'INCLUDED': 'INCLUDED', 'EXCLUDED': 'EXCLUDED', 'EXEMPT': 'EXEMPT',
                   '부가세 포함': 'INCLUDED', '부가세 별도': 'EXCLUDED', '면세': 'EXEMPT'}.get(tax_raw)
            if tax_raw is not None and tax is None:
                self.missing.append('UNSUPPORTED_TAX_MAPPING:' + qid)
            if qt is not None:
                qt['tax_basis'] = tax
            source_ref = self.extraction_source('unit_price', lambda: self.source(row.evidence_jsonb['unit_price'], row.supplier_id)) if price is not None and 'unit_price' in row.evidence_jsonb else None
            if tiers:source_ref = tier_method_ref
            transport, shipping_ref = self.extraction_source('cost_terms', lambda: self.period_cost(qid, row, 'SHIPPING'))
            handling, handling_ref = self.extraction_source('cost_terms', lambda: self.period_cost(qid, row, 'HANDLING'))
            if any(c.get('cost_type') not in ('SHIPPING', 'HANDLING') for c in row.cost_terms_jsonb):
                raise BridgeError('TEAM_OTHER_COST_CLASSIFICATION_REQUIRED')
            switching = self.fact(qid + '.switching_cost', required=row.supplier_id != incumbent_id)
            switch_ref = self.source(self.facts[qid + '.switching_cost']['evidence']) if switching is not None else None
            cost_sources = [shipping_ref, handling_ref, switch_ref]
            cost_ref = 'team-costs:' + digest(cost_sources)
            self.evidence[cost_ref] = {'source_refs': cost_sources}
            received = self.mails[row.source_mail_id].get('received_at')
            self.need(received, qid + '.received_at')
            delivery = row.checks_jsonb.get('delivery_date', {}).get('value')
            if delivery is not None:
                delivery = checked('delivery_date')
                supply_from = supply_to = delivery  # explicit single delivery date
            else:
                supply_from, supply_to = self.fact(qid + '.supply_from'), self.fact(qid + '.supply_to')
            lead = row.checks_jsonb.get('lead_time', {}).get('value')
            if isinstance(lead, str) and re.fullmatch(r'\d+일', lead):
                lead_days = int(checked('lead_time')[:-1])
            elif delivery is not None and lead is None:
                lead_days = None  # confirmed delivery date suffices; no invented zero-day lead
            else:
                lead_days = self.fact(qid + '.lead_days')
            has_switching_amount = switching is not None and decimal_value(_number(switching, qid + '.switching_cost'), 'switching_cost') != 0
            cost_scope = self.fact(qid + '.cost_scope', required=has_switching_amount)
            if cost_scope is None and not has_switching_amount:
                # SHIPPING/HANDLING were checked above for period scope/inclusion.
                # With no applicable switching amount, this label adds no new fact.
                cost_scope = 'PER_SUPPLIER_PER_PERIOD'
            q = {'supplier_id': row.supplier_id, 'quote_id': qid, 'version': row.version_no,
                 'received_on': datetime.fromisoformat(received).date().isoformat() if received else None,
                 'valid_from': self.fact(qid + '.valid_from', required=False), 'valid_until': until,
                 'supply_from': supply_from, 'supply_to': supply_to,
                 'item': qi, 'unit': unit, 'currency': currency, 'terms': qt, 'unit_price': price,
                 'moq': field('moq', pricing.get('moq')), 'available_quantity': field('available_quantity', row.available_quantity),
                 'lead_days': lead_days, 'costs': {'transport': transport,
                     'other_logistics': handling, 'switching': switching},
                 'cost_scope': cost_scope, 'cost_source_ref': cost_ref,
                 'source_ref': source_ref, 'source_verified': row.extraction_status == 'CONFIRMED',
                 'eligibility': self.fact(qid + '.eligibility')}
            q.update(self.pack_multiple(row, qid))
            if tiers:
                q.update(price_tiers=tiers, pricing_method='ALL_UNITS', tier_method_ref=tier_method_ref)
            quote_data.append(q)
            bindings[qid] = {'kind': 'CURRENT', 'supplier_id': row.supplier_id, 'quote_id': qid, 'version': row.version_no}
            if row.supplier_id == incumbent_id:
                if tiers:
                    from app.agents.kan60.purchase_plans.contract import quoted_price, n, text_number
                    basis_quantity = n(_number(case.get('quantity'), 'quantity'))
                    if q['moq'] is None or q['available_quantity'] is None:
                        raise MissingTeamData(self.missing, issues=self.issues)
                    if not n(q['moq']) <= basis_quantity <= n(q['available_quantity']):
                        raise BridgeError('KEEP_QUANTITY_NOT_FEASIBLE')
                    price = text_number(quoted_price(q, basis_quantity)[0])
                previous = [m['fields']['previous_unit_price'] for m in row.mail_normalized.values()
                            if 'previous_unit_price' in m.get('fields', {})]
                if len(previous) != 1:
                    self.issue('NEEDS_EVIDENCE' if not previous else 'RECHECK_SOURCE', 'previous_unit_price')
                else:
                    self.extraction_source('previous_unit_price', lambda: _verify_number_source('previous_unit_price', previous[0]))
                    before_ref = self.extraction_source('previous_unit_price', lambda: self.source(previous[0], row.supplier_id, 'previous_unit_price'))
                    notice = {'quote_version_id': qid, 'previous_unit_price': previous[0]['value'],
                              'requested_unit_price': price, 'before_source_ref': before_ref,
                              'requested_source_ref': source_ref, 'effective_date': effective,
                              'supplier_id': incumbent_id}
        if notice is None and not self.issues:
            self.missing.append('incumbent.previous_and_requested_price_basis')
        if self.missing or self.issues:
            raise MissingTeamData(self.missing, issues=self.issues)
        # Fields already available from team records cannot be overridden by facts.
        quantity = _number(case.get('quantity'), 'quantity')
        if decimal_value(_number(fixed['quantity'], 'fixed_commitment.quantity'), 'fixed_commitment.quantity') != 0:
            raise BridgeError('TEAM_FIXED_ALLOCATION_UNREPRESENTABLE')
        data = {'schema_version': 'kan.plan.v0', 'contract_revision': 'prototype-1', 'data_kind': 'LIVE',
                'request_id': scope.case_id, 'revision': scope.revision, 'as_of': as_of, 'period': period,
                'item': item, 'unit': unit_name(self.need(case.get('unit'), 'unit')), 'currency': quote_data[0]['currency'],
                'terms': terms, 'incumbent': {'supplier_id': incumbent_id, 'old_unit_price': notice['previous_unit_price'],
                    'requested_unit_price': notice['requested_unit_price'], 'effective_date': notice['effective_date'],
                    'source_ref': notice['requested_source_ref'], 'verified': True},
                'fixed_commitment': fixed, 'changeable_quantity': quantity, 'repricing_scope': repricing,
                'quotes': quote_data, 'policy': policy}
        for q in quote_data:
            for name in ('unit_price', 'moq', 'available_quantity'):
                if name == 'unit_price' and q.get('price_tiers'):continue
                q[name] = _number(q[name], q['quote_id'] + '.' + name)
            q['costs'] = {k: None if k == 'switching' and q['supplier_id'] == incumbent_id and v is None else
                         _number(v, q['quote_id'] + '.costs.' + k) for k, v in q['costs'].items()}
        return {'revision': scope.revision, 'data': data, 'quote_bindings': bindings,
                'notice_quote_ids': [notice['quote_version_id']], 'notice_basis': notice,
                'evidence': self.evidence, 'team_source_digest': digest({'records': team_record_digest(case, self.rows),
                    'facts': self.facts})}
