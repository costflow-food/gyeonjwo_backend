import hashlib
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.shared.infrastructure.db.contracts import REFERENCE_COLUMNS, RPC_CONTRACTS, OwnershipLost, RpcRepository
from app.shared.infrastructure.db.references import ReferenceRepository
from app.shared.infrastructure.db.storage import SourceStorage, attachment_path, mail_path
from app.main import app
from scripts.check_connection import check


def test_health_does_not_require_db_credentials():
    assert TestClient(app).get('/api/v1/health').json()['status'] == 'ok'


def test_no_credentials_is_not_a_successful_connection():
    with pytest.raises(RuntimeError):
        Settings(_env_file=None).require_server()


def test_schema_contract_counts():
    assert len(REFERENCE_COLUMNS) == 7
    assert sum(len(columns.split(',')) for columns in REFERENCE_COLUMNS.values()) == 54
    assert len(RPC_CONTRACTS) == 19


def test_rpc_uses_exact_params():
    client = MagicMock()
    data = {'work_id': 'W-20261026-001', 'run_id': '00000000-0000-0000-0000-000000000001', 'lease_expires_at': '2026-10-26T12:00:00Z'}
    client.rpc.return_value.execute.return_value.data = data
    assert RpcRepository(client).call('start_analysis', p_work_id='W-20261026-001') == data
    client.rpc.assert_called_once_with('start_analysis', {'p_work_id': 'W-20261026-001'})


@pytest.mark.parametrize('name,params', [
    ('delete_all', {}), ('complete_analysis', {'p_work_id': 'x'}),
    ('start_analysis', {'p_work_id': 'x', 'p_lease_seconds': 29}),
    ('start_analysis', {'p_work_id': 'x', 'p_lease_seconds': True}),
    ('start_analysis', {'p_work_id': 'x', 'run_id': 'wrong'}),
    ('finish_classification', {'p_work_id': 'x', 'p_classification_token': 't', 'p_status': True, 'p_reason': 'r'}),
])
def test_invalid_rpc_rejected_before_network(name, params):
    client = MagicMock()
    with pytest.raises(ValueError):
        RpcRepository(client).call(name, **params)
    client.rpc.assert_not_called()


def test_false_rpc_response_means_ownership_lost():
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = False
    with pytest.raises(OwnershipLost):
        RpcRepository(client).call('complete_analysis', p_work_id='x', p_run_id='old')


def query_client(pages):
    client = MagicMock()
    query = client.table.return_value.select.return_value
    for name in ('eq', 'is_', 'order', 'range'):
        getattr(query, name).return_value = query
    query.execute.side_effect = [SimpleNamespace(data=page) for page in pages]
    return client, query


def test_decimal_and_null_are_preserved():
    client, _ = query_client([[{'amount': 5500.25, 'coverage_quantity': None}]])
    row = ReferenceRepository(client).read('cost_terms')[0]
    assert row == {'amount': Decimal('5500.25'), 'coverage_quantity': None}


def test_reference_pagination():
    client, query = query_client([[{'supplier_id': str(i)} for i in range(100)], [{'supplier_id': 'last'}]])
    assert len(ReferenceRepository(client).read('suppliers')) == 101
    assert query.range.call_args_list[1].args == (100, 199)


def test_ambiguous_supplier_must_stop():
    client, _ = query_client([[{'supplier_id': 'a'}, {'supplier_id': 'b'}]])
    with pytest.raises(ValueError):
        ReferenceRepository(client).match_supplier('A사')


def test_unknown_reference_table_is_blocked():
    client = MagicMock()
    with pytest.raises(ValueError):
        ReferenceRepository(client).read('review_results')
    client.table.assert_not_called()


@pytest.mark.parametrize('work_id,name', [('bad', 'a.pdf'), ('W-20261026-001', '../secret'), ('W-20261026-001', 'mail.eml')])
def test_path_traversal_and_overwrite_blocked(work_id, name):
    with pytest.raises(ValueError):
        attachment_path(work_id, name)


def test_source_hash_mismatch():
    client = MagicMock()
    client.storage.from_.return_value.download.return_value = b'changed'
    with pytest.raises(ValueError, match='mismatch'):
        SourceStorage(client).download_verified(mail_path('W-20261026-001'), 8, hashlib.sha256(b'original').hexdigest())


def test_mail_upload_is_verified_and_not_overwritten():
    client = MagicMock()
    bucket = client.storage.from_.return_value
    bucket.download.return_value = b'mail'
    result = SourceStorage(client).upload_mail('W-20261026-001', b'mail')
    assert result['raw_size_bytes'] == 4
    assert bucket.upload.call_args.args[2]['upsert'] == 'false'


def test_readiness_rejects_public_bucket():
    client = MagicMock()
    client.storage.get_bucket.return_value.public = True
    with pytest.raises(RuntimeError):
        check(client)
