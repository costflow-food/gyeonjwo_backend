"""Internal send node. No HTTP/LLM permission to directly invoke Gmail send."""
import logging
from app.core.errors import ServiceError
from app.features.mailboxes.application.ingestion import message_metadata
from app.features.work_cases.domain.rfq import AuthorizedMail
from app.features.work_cases.domain.candidates import selected_for_dispatch

logger = logging.getLogger('uvicorn.error.rfq_send')


class RfqDelivery:
    def __init__(self, repo, mailbox):
        self.repo, self.mailbox = repo, mailbox

    def send(self, owner, draft_id):
        draft = self.repo.draft(owner, draft_id)
        if draft['status'] != 'DRAFT':
            # No retry of SENDING/UNKNOWN/SENT, irrespective of caller keys.
            return {'status': draft['status'], 'sent_now': False}
        case = self.repo.case(owner, draft['work_case_id'])
        if not selected_for_dispatch(draft, case):
            return {'status': 'CANDIDATE_SELECTION_REQUIRED', 'sent_now': False}
        style = draft.get('style_snapshot_jsonb') or {}
        approved = draft.get('approved_by') == owner and bool(draft.get('approved_at'))
        review_roles = {'CLARIFICATION': 'INFO_PRIORITY', 'COUNTER_OFFER': 'COUNTER_OFFER'}
        review = style.get('producer') == 'graph-review-v1' and draft.get('purpose') in review_roles
        if not review and (draft.get('purpose') != 'RFQ' or style.get('producer') != 'rfq-v1'):
            raise ServiceError('ACTION_NOT_ALLOWED', '이 발송 노드의 RFQ 초안이 아닙니다.', 409)
        run = self.repo.candidate_run(owner, style.get('producer_run_id'))
        if (run['status'] != 'COMPLETED' or run['agent_role'] != (review_roles[draft['purpose']] if review else 'CANDIDATE')
                or run['work_case_id'] != draft['work_case_id'] or run['input_revision'] != draft['input_revision']
                or (review and (str(case.get('current_batch_id')) != str(style.get('review_batch_id'))
                                or str(run.get('review_batch_id')) != str(style.get('review_batch_id'))
                                or style.get('input_revision') != case['input_revision']))
                or (not approved and draft_id not in (run.get('output_jsonb') or {}).get('draft_ids', []))):
            return {'status': 'PREPARATION_NOT_READY', 'sent_now': False}
        if not approved:
            if style.get('candidate_caution'):
                return {'status': 'WAITING_APPROVAL', 'sent_now': False}
            if style.get('auto_policy_eligible') is not True:
                return {'status': 'WAITING_APPROVAL', 'sent_now': False}
            policy = self.repo.send_policy(owner, case, draft['recipient'], draft['purpose'], draft['supplier_id'])
            if (not policy['auto_eligible'] or not style.get('auto_settings_version')
                    or policy.get('settings_version') != style['auto_settings_version']):
                return {'status': 'SETTINGS_CHANGED', 'sent_now': False}
        connection, grant = self.mailbox.authorized(owner, 'send')
        if connection['mailbox_ref'] != case['mailbox_ref']:
            raise ServiceError('MAILBOX_RECONNECT_REQUIRED', '메일함을 다시 연결해 주세요.', 409)
        claimed = self.repo.claim_send(owner, draft_id)
        if claimed is None:
            return {'status': 'WAITING_APPROVAL', 'sent_now': False}
        if not approved and claimed.settings_version != style['auto_settings_version']:
            self.repo.finish_send(owner, draft_id, 'FAILED', code='SETTINGS_CHANGED')
            return {'status': 'FAILED', 'sent_now': False}
        if (claimed.recipient != draft['recipient'] or claimed.subject != draft['subject']
                or claimed.body_text != draft['body_text']):
            # Provider has not been called: this is a proven pre-send mismatch,
            # never an uncertain send. Do not send newly changed content against
            # the policy/preparation snapshot checked above.
            self.repo.finish_send(owner, draft_id, 'FAILED', code='SEND_SNAPSHOT_CHANGED')
            return {'status': 'FAILED', 'sent_now': False}
        # Everything beyond here may follow a persisted SENDING. Never release
        # it or retry the provider POST on timeouts/malformed acknowledgements.
        try:
            response = self.mailbox.google.send_claimed(grant, claimed)
            provider_id = response.get('id') if isinstance(response, dict) else None
            if not isinstance(provider_id, str) or not provider_id.strip():
                raise ValueError('Missing provider message ID')
        except Exception:
            self.repo.finish_send(owner, draft_id, 'UNKNOWN', code='SEND_RESULT_UNKNOWN')
            logger.warning('rfq send_result_unknown')
            return {'status': 'UNKNOWN', 'sent_now': None}
        # A failed/lost finish acknowledgement MUST NOT trigger another provider
        # send or overwrite a possibly committed SENT with UNKNOWN.
        self.repo.finish_send(owner, draft_id, 'SENT', provider_id=provider_id)
        logger.info('rfq send_completed')
        return {'status': 'SENT', 'sent_now': True}

    def reconcile(self, owner, draft_id):
        draft = self.repo.draft(owner, draft_id)
        if draft['status'] != 'UNKNOWN':
            return {'status': draft['status']}
        case = self.repo.case(owner, draft['work_case_id'])
        connection, grant = self.mailbox.authorized(owner, 'read')
        if connection['mailbox_ref'] != case['mailbox_ref']:
            raise ServiceError('MAILBOX_RECONNECT_REQUIRED', '메일함을 다시 연결해 주세요.', 409)
        reference = AuthorizedMail.from_claim(draft_id, {'authorized': True, 'draft_id': draft_id,
            'recipient': draft['recipient'], 'subject': draft['subject'], 'body_text': draft['body_text']})
        # Absence is never evidence of failure. Only exact sent-mail evidence
        # can resolve UNKNOWN here; no automatic FAILED or replacement send.
        ids = self.mailbox.google.sent_by_message_id(grant, reference.rfc_message_id)
        matches = []
        for message_id in ids:
            raw = self.mailbox.google.message(grant, message_id)
            if 'SENT' not in raw.get('labelIds', []):
                continue
            mail = message_metadata(raw)
            if (mail['message_id_header'] == reference.rfc_message_id
                    and mail['sender'] == [grant.email.casefold()]
                    and mail['recipients'] == [reference.recipient]
                    and mail['subject'] == reference.subject
                    and mail['body_text'].replace('\r\n', '\n').rstrip('\n') == reference.body_text.replace('\r\n', '\n').rstrip('\n')):
                matches.append(message_id)
        if len(matches) == 1:
            self.repo.reconcile_sent(owner, draft_id, matches[0], {'method': 'RFC_MESSAGE_AND_SNAPSHOT_MATCH'})
            return {'status': 'SENT'}
        return {'status': 'UNKNOWN'}
