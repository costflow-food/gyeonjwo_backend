"""Regression tests for shared mutation request-key validation."""
import unittest

from app.core.idempotency import request_identity, valid_idempotency_key
from app.core.errors import ServiceError
from app.features.work_cases.application.actions import validate_action_key


class IdempotencyKeyValidationTests(unittest.TestCase):
    def test_accepts_normal_client_key(self):
        key = "a" * 32
        self.assertTrue(valid_idempotency_key(key))
        self.assertEqual(request_identity("owner", "POST", "/api/v1/example", key, {}).key, key)
        validate_action_key(key)

    def test_rejects_bad_keys_consistently(self):
        bad_keys = [None, "", " ", " leading", "trailing ", "has\nnewline",
                    "has\x00null", "has\x7fdelete", "a" * 201]
        for key in bad_keys:
            with self.subTest(key=repr(key)):
                self.assertFalse(valid_idempotency_key(key))
                with self.assertRaises(ServiceError):
                    request_identity("owner", "POST", "/api/v1/example", key, {})
                with self.assertRaises(ServiceError):
                    validate_action_key(key)

    def test_canonical_body_hash_ignores_mapping_order(self):
        first = request_identity("owner", "POST", "/api/v1/example", "key", {"a": 1, "b": 2})
        second = request_identity("owner", "POST", "/api/v1/example", "key", {"b": 2, "a": 1})
        self.assertEqual(first.body_hash, second.body_hash)


if __name__ == "__main__":
    unittest.main()
