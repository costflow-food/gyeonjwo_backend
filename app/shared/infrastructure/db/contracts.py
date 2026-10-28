"""Notion 06 v2.2 (2026-10-26). No result tables or direct DML."""
from datetime import date, datetime
import re
from uuid import UUID

SCHEMA_SQL_SHA256 = 'e510c9a2bc333644398be2c5469f83490caec4391ceda26dc4d000114a7eaf7d'
WORK_COLUMNS = 'work_id,received_at,sender,subject,classification_reason,notes,queue_status,item_id,classification_token,run_id,analysis_started_at,lease_expires_at,created_at,updated_at'

REFERENCE_COLUMNS = {
    'suppliers': 'supplier_id,supplier_name,certifications,experience_comment,source_ref',
    'items': 'item_id,item_name,origin,grade,processing',
    'contract_headers': 'contract_no',
    'contracts': 'contract_id,contract_no,supplier_id,item_id,valid_from,valid_to,price_rule,unit_price,adjustment_clause,source_ref',
    'purchase_plans': 'plan_id,contract_id,delivery_date,quantity,source_ref',
    'price_references': 'reference_id,reference_type,contract_id,supplier_id,item_id,reference_date,valid_until,unit_price,quantity,moq,unit,currency,lead_days,delivery_terms,trade_stage,source_ref',
    'cost_terms': 'cost_term_id,contract_no,reference_id,cost_type,amount,basis,payer,included_in_price,coverage_from,coverage_to,coverage_quantity,source_ref',
}

# Explicit RPC parameter names: (required, optional). Never expose this directly as an HTTP API.
RPC_CONTRACTS = {
    'issue_work_id': ('', ''),
    'configure_gmail_mailbox': ('p_email', 'p_initial_history_id p_watch_expires_at'),
    'find_work_item': ('p_mailbox_id p_gmail_message_id', ''),
    'register_work_item': ('p_work_id p_mailbox_id p_gmail_message_id p_received_at p_sender p_subject p_mail_body p_attachments p_raw_size_bytes p_raw_sha256', ''),
    'get_work_item_source': ('p_work_id', ''),
    'update_work_item_notes': ('p_work_id', 'p_notes'),
    'finish_classification': ('p_work_id p_classification_token p_status p_reason', ''),
    'restart_classification': ('p_work_id', ''),
    'exclude_work_item': ('p_work_id', 'p_reason'),
    'start_analysis': ('p_work_id', 'p_lease_seconds'),
    'heartbeat_analysis': ('p_work_id p_run_id', 'p_lease_seconds'),
    'set_analysis_item': ('p_work_id p_run_id p_item_id', ''),
    'complete_analysis': ('p_work_id p_run_id', ''),
    'recover_expired_analyses': ('', ''),
    'enqueue_gmail_notification': ('p_subscription p_pubsub_message_id p_email p_history_id', ''),
    'claim_gmail_notification': ('', 'p_lease_seconds'),
    'heartbeat_gmail_notification': ('p_notification_id p_claim_token', 'p_lease_seconds'),
    'finish_gmail_notification': ('p_notification_id p_claim_token p_last_history_id', ''),
    'fail_gmail_notification': ('p_notification_id p_claim_token p_error_code', 'p_retry'),
}


class OwnershipLost(RuntimeError):
    """DB rejected a stale token, expired lease or invalid state."""


BOOLEAN_RPCS = {
    'finish_classification', 'exclude_work_item', 'heartbeat_analysis',
    'set_analysis_item', 'complete_analysis', 'heartbeat_gmail_notification',
    'finish_gmail_notification', 'fail_gmail_notification', 'update_work_item_notes',
}


class WorkNotFound(RuntimeError):
    pass


class InvalidRpcResponse(RuntimeError):
    """The deployed RPC response does not match the agreed SQL contract."""


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('RPC timestamps must include a timezone')
        return value.isoformat()
    if isinstance(value, (date, UUID)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _validate_response(name, result):
    def invalid():
        raise InvalidRpcResponse(f'{name} returned an incompatible response')
    def work_id(value):
        return isinstance(value, str) and re.fullmatch(r'W-[0-9]{8}-[0-9]{3,}', value) is not None
    def uuid(value):
        try:
            UUID(str(value))
            return True
        except (ValueError, TypeError, AttributeError):
            return False
    if name == 'issue_work_id' and not work_id(result):
        invalid()
    if name == 'find_work_item' and result is not None and not work_id(result):
        invalid()
    if name in ('enqueue_gmail_notification', 'recover_expired_analyses'):
        if type(result) is not int or result < (1 if name == 'enqueue_gmail_notification' else 0):
            invalid()
    if name == 'restart_classification' and not uuid(result):
        invalid()
    if name == 'configure_gmail_mailbox' and not isinstance(result, dict):
        invalid()
    if name == 'register_work_item':
        if not isinstance(result, dict) or not work_id(result.get('work_id')) or type(result.get('created')) is not bool:
            invalid()
        if result['created'] and not uuid(result.get('classification_token')):
            invalid()
    if name == 'start_analysis':
        if not isinstance(result, dict) or not work_id(result.get('work_id')) or not uuid(result.get('run_id')):
            invalid()
        try:
            expires = datetime.fromisoformat(result['lease_expires_at'].replace('Z', '+00:00'))
            if expires.utcoffset() is None:
                invalid()
        except (ValueError, TypeError, KeyError, AttributeError):
            invalid()
    if name == 'get_work_item_source' and result is not None:
        if not isinstance(result, dict) or not isinstance(result.get('mail_body'), str) or not isinstance(result.get('attachments'), list):
            invalid()
        if type(result.get('raw_size_bytes')) is not int or not 0 < result['raw_size_bytes'] <= 52_428_800:
            invalid()
        if not isinstance(result.get('raw_sha256'), str) or not re.fullmatch('[a-f0-9]{64}', result['raw_sha256']):
            invalid()
    if name == 'claim_gmail_notification' and result is not None:
        required = {'notification_id', 'mailbox_id', 'email_address', 'last_history_id', 'target_history_id', 'claim_token', 'lease_expires_at', 'attempt_count'}
        if not isinstance(result, dict) or not required <= result.keys() or not uuid(result['claim_token']) or not uuid(result['mailbox_id']):
            invalid()
        for key in ('last_history_id', 'target_history_id'):
            value = result[key]
            if key == 'last_history_id' and value is None:
                continue
            if type(value) is int:
                value = str(value)
            if not isinstance(value, str) or not re.fullmatch('[0-9]{1,30}', value):
                invalid()
            result[key] = value
    return result


class RpcRepository:
    def __init__(self, client):
        self.client = client

    def call(self, name: str, **params):
        if name not in RPC_CONTRACTS:
            raise ValueError('Unknown server RPC')
        required, optional = (set(part.split()) for part in RPC_CONTRACTS[name])
        if required - params.keys() or params.keys() - required - optional:
            raise ValueError(f'Invalid parameters for {name}')
        if 'p_lease_seconds' in params:
            lease = params['p_lease_seconds']
            if type(lease) is not int or not 30 <= lease <= 3600:
                raise ValueError('Lease must be an integer between 30 and 3600 seconds')
        if name == 'finish_classification' and params['p_status'] not in ('TRUE', 'FALSE', 'PENDING'):
            raise ValueError('Classification status must be a string TRUE/FALSE/PENDING')
        if name == 'update_work_item_notes':
            notes = params.get('p_notes')
            if notes is not None and (not isinstance(notes, str) or len(notes) > 8192):
                raise ValueError('Notes must be null or a string of at most 8192 characters')
        result = self.client.rpc(name, _json_value(params)).execute().data
        if name in BOOLEAN_RPCS:
            if result is False:
                if name == 'update_work_item_notes':
                    raise WorkNotFound('The work item does not exist')
                raise OwnershipLost(f'{name} was rejected; do not continue this operation')
            if result is not True:
                raise InvalidRpcResponse(f'{name} must return a JSON boolean')
        return _validate_response(name, result)
