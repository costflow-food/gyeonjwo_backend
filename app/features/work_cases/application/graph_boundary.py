"""Backend-owned START adapter; later entry points need their own storage contract."""
import inspect
import logging
from contextlib import ExitStack
import httpx
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.agent_store import ScopedStore
from app.features.work_cases.infrastructure.graph_store import GraphStore

logger = logging.getLogger('uvicorn.error.case_graph')


def graph_contract(*, review_enabled=False):
    try:
        from app.agents import agents, graph, nodes, schemas, tools
        for obj, names in ((tools, ('use_case_store', 'use_quote_store', 'use_candidate_store', 'use_draft_store')),
                           (nodes, ('use_document_store',)), (graph, ('build_graph', 'run_case'))):
            if any(not callable(getattr(obj, name, None)) for name in names):
                raise ValueError('Missing graph protocol')
        if review_enabled and (not callable(getattr(tools, 'use_priority_store', None))
                               or not callable(getattr(nodes, 'use_card_store', None))):
            raise ValueError('Missing review graph stores')
        if not {'model', 'runs', 'role_models'} <= set(inspect.signature(agents.AgentRunner).parameters):
            raise ValueError('Incompatible runner')
        if not {'run_agent', 'current_revision', 'latest_quotes'} <= set(inspect.signature(graph.build_graph).parameters):
            raise ValueError('Incompatible graph')
        return agents, graph, nodes, schemas, tools
    except Exception:
        raise ServiceError('AGENT_INTEGRATION_REQUIRED', '에이전트 그래프 공개 인터페이스를 확인해야 합니다.',
                           503, retryable=False, next_action='CONTACT_SERVER_ADMIN') from None


def run_graph(*, settings, scope, runner_factory=None, store_factory=GraphStore):
    mode = (scope.job or {}).get('start_from')
    review = (scope.job or {}).get('execution_mode') == 'GRAPH_REVIEW_V1'
    if mode not in {'START', 'REPLY', 'PLAN'} or (mode != 'START' and not review) or (mode == 'START' and review):
        raise ServiceError('GRAPH_ENTRYPOINT_NOT_CONNECTED',
                           '이 검토 재개 경로의 서버 연결이 아직 준비되지 않았습니다.',
                           503, retryable=False, next_action='CONTACT_SERVER_ADMIN')
    agents, graph, nodes, schemas, tools = graph_contract(review_enabled=review)
    if review and store_factory is GraphStore:
        from app.features.work_cases.infrastructure.review_graph_store import ReviewGraphStore
        store = ReviewGraphStore(scope, scope.job)
    else:
        store = store_factory(scope, scope.job)
    scope.agent_store = store
    proxy = ScopedStore()
    for hook in (tools.use_case_store, tools.use_quote_store, tools.use_candidate_store,
                 tools.use_draft_store, nodes.use_document_store):
        hook(proxy)
    if review:
        tools.use_priority_store(proxy)
        nodes.use_card_store(proxy)
    # No shared user's store in globals. Every protocol call is routed through
    # CURRENT_SCOPE. No stop-after-extraction option is added to the graph.
    def bounded_request(request):
        remaining = scope.remaining_seconds()
        request.extensions['timeout'] = {key: min(settings.agent_graph_request_timeout_seconds, remaining)
                                         for key in ('connect', 'read', 'write', 'pool')}

    with ExitStack() as stack:
        if runner_factory:
            runner = runner_factory(store)
        else:
            client = stack.enter_context(httpx.Client(event_hooks={'request': [bounded_request]}))
            def model(role=None, verifier=False):
                result = agents.chat_model(settings, role, verifier=verifier)
                original = result.root_client
                result.root_client = original.with_options(http_client=client, max_retries=0,
                    timeout=settings.agent_graph_request_timeout_seconds)
                original.close()
                stack.callback(result.root_client.close)
                result.client = result.root_client.chat.completions
                result.max_retries = 0
                return result
            roles = tuple(schemas.AgentRole)
            runner = agents.AgentRunner(model(), runs=store, role_models={role: model(role) for role in roles},
                                        verifiers={role: model(role, verifier=True) for role in roles
                                                   if str(role) in settings.llm_verifier_reasoning_efforts})

        def run_role(role, data):
            try:
                scope.check(str(data.case_id), data.input_revision)
                result = runner(role, data)
                scope.check(scope.case_id, scope.revision)
                return result
            except ServiceError as error:
                # Agent graph converts exceptions to FAILED. Keep a safe reason
                # for a host operator without mail/model content or raw errors.
                safe = error.code if error.code in {'ACTION_NOT_ALLOWED', 'REVISION_CONFLICT',
                    'EXTRACTION_TIME_LIMIT', 'EXTRACTION_LEASE_LOST', 'PROCESSING_UNAVAILABLE'} else 'GRAPH_ROLE_FAILED'
                logger.warning('graph role_failed role=%s code=%s', str(role), safe)
                raise
            except Exception as error:
                # The agent graph logs the exception message. Pass only a fixed
                # code across that boundary, never provider or tool output.
                safe = {
                    'ToolCallLimitExceededError': 'AGENT_TOOL_CALL_LIMIT',
                    'UngroundedOutputError': 'AGENT_UNGROUNDED_OUTPUT',
                    'ValidationError': 'AGENT_VALIDATION_FAILED',
                    'APITimeoutError': 'AGENT_PROVIDER_TIMEOUT',
                }.get(type(error).__name__, 'GRAPH_ROLE_FAILED')
                logger.warning('graph role_failed start_from=%s role=%s code=%s', mode, str(role), safe)
                raise RuntimeError(safe) from None

        app_graph = graph.build_graph(run_agent=run_role, current_revision=store.current_input_revision,
                                      latest_quotes=store.latest_ids)
        output = graph.run_case(app_graph, schemas.RunCaseInput(case_id=scope.case_id,
            start_from=schemas.StartFrom(mode), input_revision=scope.revision, mail_ids=list(scope.mail_ids)))
        if str(output.case_id) != scope.case_id or output.input_revision != scope.revision:
            raise ValueError('Changed graph result scope')
        if output.status != schemas.RunStatus.STALE:
            scope.check(scope.case_id, scope.revision)
        if not review and (output.card_id is not None or output.status == schemas.RunStatus.COMPLETE):
            raise ValueError('START cannot create a review card')
        if review and output.status == schemas.RunStatus.COMPLETE and (not store.card_id or output.card_id != store.card_id):
            raise ValueError('Unconfirmed review card')
        if output.status == schemas.RunStatus.WAITING_REPLY:
            # Graph output can contain only actual durable drafts from this run.
            ids = [str(value) for value in output.draft_ids]
            if not ids or len(ids) != len(set(ids)) or any(value not in store.draft_ids for value in ids):
                raise ValueError('Unconfirmed graph drafts')
        return output
