"""KAN-288 거래처 수정: 입력 검증과 DB 오류 → API 오류 변환."""
import unittest
from uuid import uuid4

from postgrest.exceptions import APIError
from pydantic import ValidationError

from app.core.errors import ServiceError
from app.features.suppliers.registration import SupplierUpdateInput
from app.features.suppliers.repository import SupplierRepository

SUPPLIER = str(uuid4())
ITEM = str(uuid4())


class Call:
    def __init__(self, result):
        self.result = result

    def execute(self):
        if isinstance(self.result, Exception):
            raise self.result
        return type('Response', (), {'data': self.result})()


class Client:
    def __init__(self, result):
        self.result, self.calls = result, []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return Call(self.result)


class Repository(SupplierRepository):
    def detail(self, supplier_id):
        return {'supplier_id': supplier_id, 'detail': True}


def db_error(code, message='x'):
    return APIError({'code': code, 'message': message, 'details': None, 'hint': None})


class UpdateInputTests(unittest.TestCase):
    def test_only_sent_keys_reach_the_database(self):
        body = SupplierUpdateInput.model_validate({'name': '  새 이름  ', 'location': None}).body()
        self.assertEqual(body, {'name': '새 이름', 'location': None})

    def test_email_is_normalized_and_empty_clears(self):
        self.assertEqual(SupplierUpdateInput.model_validate({'contact_email': ' A@B.kr '}).body(),
                         {'contact_email': 'a@b.kr'})
        self.assertEqual(SupplierUpdateInput.model_validate({'contact_email': ''}).body(), {'contact_email': None})

    def test_item_update_and_new_item(self):
        body = SupplierUpdateInput.model_validate({'items': [
            {'supplier_item_id': ITEM, 'unit': 'box', 'units_per_box': '10', 'notes': ''},
            {'item_name': '냉동 망고', 'certifications': ['HACCP']},
        ]}).body()
        self.assertEqual(body['items'][0], {'supplier_item_id': ITEM, 'unit': 'box', 'units_per_box': '10', 'notes': ''})
        self.assertEqual(body['items'][1], {'item_name': '냉동 망고', 'certifications': ['HACCP']})

    def test_rejects_bad_input(self):
        bad = [
            {},
            {'name': None},
            {'name': ''},
            {'owner_user_id': SUPPLIER},
            {'contact_email': 'not-an-address'},
            {'name': 'a\x00b'},
            {'items': None},
            {'items': [{}]},
            {'items': [{'item_name': ''}]},
            {'items': [{'supplier_item_id': ITEM, 'item_name': None}]},
            {'items': [{'supplier_item_id': 'nope', 'unit': 'kg'}]},
            {'items': [{'item_name': 'a', 'units_per_box': '-1'}]},
            {'items': [{'item_name': 'a', 'units_per_box': '0'}]},
            {'items': [{'item_name': 'a', 'weight_per_box': 'abc'}]},
            {'items': [{'item_name': 'a', 'weight_per_box': 3}]},
            {'items': [{'item_name': 'a', 'certifications': 'HACCP'}]},
            {'items': [{'item_name': 'a', 'delete': True}]},
            {'items': [{'supplier_item_id': ITEM, 'unit': 'kg'}, {'supplier_item_id': ITEM, 'unit': 'box'}]},
            {'items': [{'item_name': f'품목{n}'} for n in range(51)]},
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                SupplierUpdateInput.model_validate(value)


class UpdateRepositoryTests(unittest.TestCase):
    def test_success_returns_detail_and_passes_owner(self):
        client = Client({'id': SUPPLIER, 'name': 'a', 'item_names': []})
        result = Repository(client, 'owner-1').update(SUPPLIER, {'name': 'a'})
        self.assertEqual(result, {'supplier_id': SUPPLIER, 'detail': True})
        self.assertEqual(client.calls, [('update_supplier',
                                         {'p_owner': 'owner-1', 'p_supplier_id': SUPPLIER, 'p_body': {'name': 'a'}})])

    def test_database_errors_become_api_errors(self):
        cases = [
            (db_error('P0002', 'SUPPLIER_NOT_FOUND'), 'RESOURCE_NOT_FOUND', 404),
            (db_error('23505'), 'SUPPLIER_ALREADY_REGISTERED', 409),
            (db_error('55006', 'SUPPLIER_EMAIL_IN_USE'), 'SUPPLIER_EMAIL_LOCKED', 409),
            (db_error('0A000', 'SUPPLIER_NOT_EDITABLE'), 'SUPPLIER_NOT_EDITABLE', 409),
            (db_error('22023', 'Invalid supplier input'), 'VALIDATION_ERROR', 422),
            (db_error('22023', 'ITEM_RENAME_NOT_ALLOWED'), 'SUPPLIER_ITEM_RENAME_NOT_ALLOWED', 422),
            (db_error('22023', 'ITEM_ALREADY_EXISTS'), 'SUPPLIER_ITEM_ALREADY_EXISTS', 422),
            (db_error('42501', 'permission denied'), 'PROCESSING_UNAVAILABLE', 503),
            (RuntimeError('network'), 'PROCESSING_UNAVAILABLE', 503),
        ]
        for error, code, status in cases:
            with self.subTest(code=code), self.assertRaises(ServiceError) as raised:
                Repository(Client(error), 'owner-1').update(SUPPLIER, {'name': 'a'})
            self.assertEqual((raised.exception.code, raised.exception.status), (code, status))

    def test_unexpected_response_is_not_reported_as_success(self):
        for data in (None, [], {'id': str(uuid4())}):
            with self.subTest(data=data), self.assertRaises(ServiceError) as raised:
                Repository(Client(data), 'owner-1').update(SUPPLIER, {'name': 'a'})
            self.assertEqual(raised.exception.status, 503)


if __name__ == '__main__':
    unittest.main()
