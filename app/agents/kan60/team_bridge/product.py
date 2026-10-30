"""Four role-owned tools behind the team's normal registry and RunContext.

No registry/prompt mutation, fixture loading, model construction or DB connection.
The backend supplies an authenticated EvidenceRepository at process composition.
This private snapshot/reference port is NOT the team's CaseStore/QuoteStore contract.
"""
from langchain_core.messages import HumanMessage

from app.agents.schemas import AGENT_IO, AgentRole
from .repository import BridgeError, EvidenceRepository, Scope
from .service import BridgeService, wire
from .team_data import MissingTeamData

_review_repository: EvidenceRepository | None = None


def use_review_repository(repository: EvidenceRepository | None) -> None:
    """Configure at backend startup, like use_case_store. None fails closed.

    Implementations must check authenticated ownership and atomically enforce
    revision on writes. Do not swap this process dependency during active runs.
    """
    global _review_repository
    _review_repository = repository


def _bound_service(runtime, role, expected_role):
    if role != expected_role:
        raise BridgeError('TOOL_ROLE_DENIED')
    if _review_repository is None or runtime.context is None:
        raise MissingTeamData(['ReviewRepository' if _review_repository is None else 'RunContext'])
    context = runtime.context
    # AgentRunner supplies the initial serialized *In. Use it to preserve the
    # exact quote scope; later model/tool messages cannot expand that scope.
    first = next((m for m in runtime.state.get('messages', []) if isinstance(m, HumanMessage)), None)
    if first is None or not isinstance(first.content, str):
        raise BridgeError('TRUSTED_AGENT_INPUT_REQUIRED')
    data = AGENT_IO[role][0].model_validate_json(first.content)
    if str(data.case_id) != context.case_id or data.input_revision != context.input_revision:
        raise BridgeError('RUN_CONTEXT_MISMATCH')
    scope = Scope.from_input(data)
    repository = (_review_repository.for_run(context) if hasattr(_review_repository, 'for_run')
                  else _review_repository)
    repository.snapshot(scope)  # ownership/revision/exact quote binding
    return BridgeService(repository, draft_model=None), scope, data


def dispatch_review(runtime, role, expected_role, operation):
    try:
        service, scope, data = _bound_service(runtime, role, expected_role)
        if role == AgentRole.RESPONSE_PLAN:
            pending = [q.field for q in data.questions if q.priority != 'LOW_VALUE']
            if pending:
                internal = [f for f in pending if f in ('policy', 'create_mail_draft', 'check_comparability',
                    'fixed_commitment', 'repricing_scope', 'as_of') or
                    f.startswith(('policy.', 'review_facts.', 'ValidationStore', 'CaseStore', 'QuoteStore', 'RunContext'))]
                if internal:
                    raise MissingTeamData(internal)
                return {'status': 'NEEDS_EVIDENCE', 'next_action': 'NEEDS_EVIDENCE',
                        'unknown_fields': pending, 'send_authorized': False}
        result = operation(service, scope, data)
        service.repo.snapshot(scope)
        return result
    except MissingTeamData as exc:
        return {'status': exc.action, 'next_action': exc.action, 'error': exc.code,
                'unknown_fields': exc.fields, 'blocking_issues': exc.issues, 'send_authorized': False}
    except BridgeError as exc:
        if exc.code in ('REVIEW_REPOSITORY_NOT_CONFIGURED', 'RUN_CONTEXT_MISMATCH',
                        'TOOL_ROLE_DENIED', 'TRUSTED_AGENT_INPUT_REQUIRED'):
            raise
        if exc.code.startswith(('STALE', 'CASE_SCOPE', 'REFERENCE_SCOPE', 'REFERENCE_SOURCE', 'TOOL_SCOPE')):
            raise
        # Do not invent extraction field names for RECHECK_SOURCE. Missing data
        # and unsupported mappings remain explicit holds for the owner to resolve.
        field = exc.code.split(':', 1)[1] if exc.code.startswith('INPUT_') and ':' in exc.code else exc.code
        return {'status': 'HOLD', 'next_action': 'HOLD', 'error': exc.code,
                'unknown_fields': [field], 'send_authorized': False}


def validate_plans(service, scope, plans):
    try:
        checked, ref, _ = service.validate_plans(scope, plans)
        return {'plan_check': 'PASS', 'plans': [wire(p) for p in checked], 'plan_set_ref': ref}
    except BridgeError as exc:
        if exc.code in ('EMPTY_PLANS', 'PLAN_SCHEMA_INVALID', 'PLAN_ALLOCATION_TOTAL_OR_SET_MISMATCH',
                        'PLAN_REFERENCE_MISMATCH'):
            return {'plan_check': 'FIX', 'reason': exc.code, 'plans': []}
        raise


def calculate_offer(service, scope, data, plans):
    # ⑤ must use the complete verified set supplied by the real AgentRunner,
    # not a subset selected in a model-generated tool argument.
    from app.agents.kan60.counter_offer.contract import digest
    if sorted(map(digest, plans)) != sorted(digest(wire(p)) for p in data.validated_plans):
        raise BridgeError('COUNTER_INPUT_PLAN_SET_MISMATCH')
    return service.calculate(scope, plans)
