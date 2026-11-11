from app.api.responses import Envelope
from app.core.errors import ServiceError
from app.core.idempotency import request_identity
from app.core.request_context import current_request_id
from app.features.work_cases.domain.candidates import CandidatesResponse, CandidateSelectionResponse
from app.features.work_cases.infrastructure.rfq_delivery_repository import RfqDeliveryRepository


def candidate_block(case, snapshot, readiness):
    if case.get('archived_at') is not None or case.get('decision') in {'ACCEPT_INCREASE', 'CONFIRM_REVIEW'}:
        return 'ACTION_NOT_ALLOWED', '보관·종료된 업무의 후보는 선택할 수 없습니다.'
    if snapshot is None:
        return 'CANDIDATES_NOT_READY', '후보 세트가 아직 준비되지 않았습니다.'
    if snapshot.revision != case['input_revision'] or str(snapshot.review_batch_id) != str(case.get('current_batch_id')):
        return 'REVISION_CONFLICT', '입력이 바뀌어 후보를 다시 확인해야 합니다.'
    if case.get('status') != 'Running' or case.get('processing_step') != 'CANDIDATES':
        return 'ACTION_NOT_ALLOWED', '현재 단계에서는 견적 요청 대상을 선택할 수 없습니다.'
    if not any(item.selectable for item in snapshot.items):
        return 'INVALID_CANDIDATE_SELECTION', '선택할 수 있는 후보가 없습니다.'
    if not readiness.available:
        return readiness.code, readiness.message
    return None


def apply_candidate_action(case, actions, disabled, block):
    if case.get('processing_step') not in {'CANDIDATES', 'SELECTING_CANDIDATES'}:
        return
    if case.get('archived_at') is not None:
        block = ('ACTION_NOT_ALLOWED', '보관된 업무의 후보는 선택할 수 없습니다.')
    if block:
        disabled.append({'action': 'SELECT_CANDIDATES', 'reason_code': block[0], 'message': block[1]})
    else:
        actions.append('SELECT_CANDIDATES')


class CandidateService:
    def __init__(self, repo, policy_repo=None):
        self.repo = repo
        self.policy_repo = policy_repo or RfqDeliveryRepository(repo.client)

    def get(self, owner, case_id):
        case = self.repo.case(owner, case_id)
        snapshot = self.repo.latest(owner, case_id)
        selection = self.repo.selection(owner, case_id, snapshot.candidate_set_id)
        if selection and (selection.revision != snapshot.revision
                          or not set(selection.selected_supplier_ids) <= {item.supplier_id for item in snapshot.items}):
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 이력의 범위를 확인할 수 없습니다.', 503)
        block = candidate_block(case, snapshot, self.repo.ready())
        if selection is not None:
            block = ('ACTION_NOT_ALLOWED', '이미 선택한 요청 대상입니다. 현재 초안·발송 상태를 확인해 주세요.')
        return CandidatesResponse(**snapshot.model_dump(),
            selected_supplier_ids=selection.selected_supplier_ids if selection else [],
            selection_revision=selection.selection_revision if selection else 0,
            is_stale=(snapshot.revision != case['input_revision'] or str(snapshot.review_batch_id) != str(case.get('current_batch_id'))),
            allowed_actions=[] if block else ['SELECT_CANDIDATES'],
            disabled_actions=[{'action': 'SELECT_CANDIDATES', 'reason_code': block[0], 'message': block[1]}] if block else [])

    def submit(self, owner, case_id, payload, key, *, dispatch_ready=False):
        body = payload.model_dump(mode='json')
        identity = request_identity(str(owner), 'PUT', f'/api/v1/work-cases/{case_id}/candidate-selection', key, body)
        case = self.repo.case(owner, case_id)  # Foreign/missing remains generic 404.
        result = self.repo.receipt(identity, case_id)
        snapshot = None
        policies = {}
        if result is None:
            if payload.expected_revision != case['input_revision']:
                raise ServiceError('REVISION_CONFLICT', '새 입력이 반영됐습니다. 후보를 다시 확인해 주세요.', 409,
                                   current_revision=case['input_revision'], retryable=False)
            snapshot = self.repo.latest(owner, case_id)
            if snapshot.candidate_set_id != payload.candidate_set_id:
                raise ServiceError('CANDIDATES_CHANGED', '후보 세트가 바뀌었습니다. 다시 확인해 주세요.', 409)
            block = candidate_block(case, snapshot, self.repo.ready())
            if block:
                status = 503 if block[0] in {'CANDIDATE_SELECTION_INTEGRATION_REQUIRED', 'PROCESSING_UNAVAILABLE'} else 409
                raise ServiceError(block[0], block[1], status, retryable=status == 503)
            if self.repo.selection(owner, case_id, snapshot.candidate_set_id) is not None:
                raise ServiceError('ACTION_NOT_ALLOWED', '이미 요청 대상을 선택했습니다. 현재 초안·발송 상태를 확인해 주세요.', 409)
            candidates = {item.supplier_id: item for item in snapshot.items}
            for supplier_id in payload.selected_supplier_ids:
                item = candidates.get(supplier_id)
                if item is None or not item.selectable:
                    raise ServiceError('INVALID_CANDIDATE_SELECTION', '현재 후보 중 필수 조건과 연락처를 확인한 업체를 선택해 주세요.',
                                       422, fields=['body.selected_supplier_ids'], retryable=False)
                policy = self.policy_repo.send_policy(owner, case, item.contact_email, 'RFQ', item.supplier_id)
                policies[str(supplier_id)] = policy
                if not item.caution and policy.get('auto_eligible') is True and not dispatch_ready:
                    raise ServiceError('PROCESSING_UNAVAILABLE', '자동 발송 워커가 준비되지 않았습니다. 같은 요청으로 다시 확인해 주세요.', 503)
            result = self.repo.submit(identity, case_id, body, dispatch_ready, current_request_id())
        try:
            if result['kind'] not in {'ACCEPTED', 'REPLAY'} or result['http_status'] != 200:
                raise ValueError('Invalid selection response')
            envelope = Envelope[CandidateSelectionResponse].model_validate(result['response']).model_dump(mode='json')
            data = envelope['data']
            selected = {str(value) for value in payload.selected_supplier_ids}
            if (data['case_id'] != str(case_id) or data['candidate_set_id'] != str(payload.candidate_set_id)
                    or data['revision'] != payload.expected_revision or not envelope['meta']['request_id']
                    or set(data['selected_supplier_ids']) != selected or len(data['selected_supplier_ids']) != len(selected)
                    or {draft['supplier_id'] for draft in data['drafts']} != selected or len(data['drafts']) != len(selected)
                    or len({draft['draft_id'] for draft in data['drafts']}) != len(selected)):
                raise ValueError('Invalid selection scope')
            if result['kind'] != 'REPLAY':
                items = {str(item.supplier_id): item for item in snapshot.items}
                for draft in data['drafts']:
                    if draft['dispatch_state'] == 'AUTO_QUEUED' and (not dispatch_ready or items[draft['supplier_id']].caution
                            or policies[draft['supplier_id']].get('auto_eligible') is not True):
                        raise ValueError('Unsafe auto dispatch result')
            return envelope
        except (ValueError, KeyError, TypeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 접수 결과를 확인할 수 없습니다.', 503) from None
