"""Backend-owned adapter to the team's public AgentRunner and store protocols.

Only INFO_EXTRACTOR is invoked here.
QuoteStore writes full validated quotes atomically; completion uses their real IDs.
"""
import inspect
import httpx
from app.core.errors import ServiceError
from app.features.work_cases.domain.extraction_outcome import ExtractionOutcome
from app.features.work_cases.infrastructure.agent_store import AgentStore, ScopedStore


def agent_contract():
    try:
        from app.agents import agents, schemas, tools
        if not all(callable(getattr(tools,name,None)) for name in ('use_case_store','use_quote_store')):
            raise ValueError('Missing store hooks')
        if not {'model','runs'} <= set(inspect.signature(agents.AgentRunner).parameters):
            raise ValueError('Incompatible AgentRunner')
        if 'INFO_EXTRACTOR' not in schemas.AgentRole.__members__:
            raise ValueError('Missing extraction role')
        return agents,schemas,tools
    except Exception:
        raise ServiceError('AGENT_INTEGRATION_REQUIRED','에이전트 공개 인터페이스를 확인해야 합니다.',503,
            retryable=False,next_action='CONTACT_SERVER_ADMIN') from None


def load_extraction_adapter():
    agent_contract()
    return run_extraction


def extraction_adapter_available():
    try:
        agent_contract()
        return True
    except ServiceError:
        return False


def run_extraction(*, settings, scope, runner_factory=None):
    agents,schemas,tools=agent_contract()
    store=AgentStore(scope,scope.job)
    scope.agent_store=store
    # Global references point to a context-aware proxy, never a user's store.
    proxy=ScopedStore()
    tools.use_case_store(proxy)
    tools.use_quote_store(proxy)
    role=schemas.AgentRole.INFO_EXTRACTOR
    data=schemas.ExtractionIn(case_id=scope.case_id,input_revision=scope.revision,mail_ids=list(scope.mail_ids))
    scope.check(scope.case_id,scope.revision)
    def bounded_request(request):
        remaining=scope.remaining_seconds()
        request.extensions['timeout']={key:min(20,remaining) for key in ('connect','read','write','pool')}
    with httpx.Client(event_hooks={'request':[bounded_request]}) as client:
        if runner_factory is not None:
            runner=runner_factory(store)
        else:
            model=agents.chat_model(settings,role)
            # Preserve the team's role/model/reasoning options; bound only I/O
            # and disable provider POST retries for the backend execution budget.
            original_client=model.root_client
            model.root_client=original_client.with_options(http_client=client,max_retries=0,timeout=20)
            original_client.close()
            model.client=model.root_client.chat.completions
            model.max_retries=0
            runner=agents.AgentRunner(model,runs=store)
        output=runner(role,data)
    scope.check(scope.case_id,scope.revision)
    if not isinstance(output,schemas.ExtractionOut):
        raise ValueError('Invalid extractor output')
    scope.agent_metadata={'model_id':store.model_id or settings.llm_model_for(role),
                          'prompt_version':store.prompt_version or 'agent-owned',
                          'agent_output':store.final_output}
    step={'role':'INFO_EXTRACTOR','next_action':str(output.next_action),
          'reason':output.reason,'unknown_fields':output.unknown_fields}
    if output.next_action=='HOLD':
        return ExtractionOutcome(case_id=scope.case_id,input_revision=scope.revision,status='ON_HOLD',
            processing_step='ON_HOLD',unresolved=output.unknown_fields,steps=[step])
    ids=list(dict.fromkeys(output.quote_version_ids))
    # Existing same-content records can be reused by the team's tool, but only
    # when loaded by this scoped store and sourced from this execution's mail.
    if not ids or any(q not in store.records or store.records[q].source_mail_id not in scope.mail_ids for q in ids):
        raise ValueError('Unconfirmed quote IDs')
    scope.full_quote_ids=ids
    item=next((store.records[q].case_conditions.get('item_name') for q in ids
               if store.records[q].case_conditions and store.records[q].case_conditions.get('item_name')),None)
    item=item or store.get_work_case(scope.case_id).get('item_name')
    return ExtractionOutcome(case_id=scope.case_id,input_revision=scope.revision,
        status='EXTRACTION_READY' if item else 'NEEDS_INFO',processing_step='CANDIDATES' if item else 'NEEDS_INFO',
        unresolved=output.unknown_fields,steps=[step])
