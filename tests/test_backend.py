"""Offline contract tests. FakeGateway never contacts the production project."""

from copy import deepcopy
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from io import BytesIO
import json
import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from offline_client import client_for
from pydantic import ValidationError

from costflow.config import Settings
from costflow.core import FIELD_NAMES, iso_date, landed_price, number, validate_quote
from costflow.errors import AppError
from costflow.extraction import Document, extract_fields, read_documents
from costflow.gateway import NoRedirect, SupabaseGateway, eq
from costflow.main import create_app
from costflow.models import FieldsRequest

USER = "11111111-1111-4111-8111-111111111111"
REVIEWER = "22222222-2222-4222-8222-222222222222"


def extracted(lines=1):
    values = {
        "item_name": "Rice",
        "specification": "1 kg",
        "processing": "raw",
        "requested_quantity": "10",
        "currency": "KRW",
        "unit": "kg",
    }
    return [
        {
            "line_no": i,
            "field_name": f,
            "raw_value": values.get(f),
            "source_location": "manual:verified",
        }
        for i in range(1, lines + 1)
        for f in FIELD_NAMES
    ]


class FakeGateway:
    """Small protocol fake; PostgreSQL trigger behavior is verified separately in DB."""

    def __init__(self, settings):
        self.settings = settings
        self.tables = {
            name: []
            for name in [
                "work_items",
                "request_fields",
                "review_results",
                "suppliers",
                "items",
                "contracts",
                "purchase_plans",
                "price_references",
                "cost_terms",
            ]
        }
        self.objects = {}
        self.calls = []
        self.next_id = 0

    def user(self, token):
        if token == "reviewer-token":
            return {"id": REVIEWER}
        if token == "user-token":
            return {"id": USER}
        if token == "outsider-token":
            return {"id": "33333333-3333-4333-8333-333333333333"}
        raise AppError(401, "invalid_credentials", "Invalid test token")

    def token(self, grant_type, body):
        return {"access_token": "user-token", "refresh_token": "refresh-test", "expires_in": 3600}

    def logout(self, token):
        self.calls.append(("logout", token))

    def rows(self, table, *, filters=None, limit=100, offset=0, order=None):
        result = deepcopy(self.tables[table])
        for key, expression in (filters or {}).items():
            if expression.startswith("eq."):
                value = expression[3:]
                if value.startswith('"'):
                    value = json.loads(value)
                result = [r for r in result if str(r.get(key)) == value]
            elif expression.startswith("lte."):
                result = [r for r in result if r[key] <= expression[4:]]
            elif expression.startswith("gte."):
                result = [r for r in result if r[key] >= expression[4:]]
            elif expression.startswith("in.("):
                values = json.loads("[" + expression[4:-1] + "]")
                result = [r for r in result if r[key] in values]
        if order:
            for field in reversed(order.split(",")):
                name, _, direction = field.partition(".")
                result.sort(key=lambda r: r[name], reverse=direction == "desc")
        return result[offset : offset + limit]

    def insert_work(self, body):
        self.tables["work_items"].append(deepcopy(body))
        return deepcopy(body)

    def rpc(self, name, body):
        self.calls.append((name, deepcopy(body)))
        if name == "issue_work_id":
            self.next_id += 1
            return f"W-20261019-{self.next_id:03}"
        work_id = body["p_work_id"]
        work = next(w for w in self.tables["work_items"] if w["work_id"] == work_id)
        if work["status"] != "ON_HOLD":
            raise AppError(409, "database_conflict", "Closed")
        if name == "save_request_fields":
            if any(r["work_id"] == work_id for r in self.tables["review_results"]):
                raise AppError(409, "database_conflict", "Extraction frozen")
            self.tables["request_fields"] = [
                r for r in self.tables["request_fields"] if r["work_id"] != work_id
            ]
            self.tables["request_fields"].extend(
                {"work_id": work_id} | r for r in deepcopy(body["p_rows"])
            )
            return len(body["p_rows"])
        self.tables["review_results"].append(
            {
                "work_id": work_id,
                "line_no": body["p_line_no"],
                "reference_id": body["p_reference_id"],
                "summary": body["p_summary"],
            }
        )
        lines = {r["line_no"] for r in self.tables["request_fields"] if r["work_id"] == work_id}
        decided = {r["line_no"] for r in self.tables["review_results"] if r["work_id"] == work_id}
        if lines == decided:
            work["status"] = "APPROVED"

    def reject(self, work_id, reason):
        work = next(w for w in self.tables["work_items"] if w["work_id"] == work_id)
        if any(r["work_id"] == work_id for r in self.tables["review_results"]):
            raise AppError(409, "database_conflict", "Decisions already exist")
        work.update(status="REJECTED", classification_reason=reason)
        return [deepcopy(work)]

    def upload(self, path, data, content_type):
        self.objects[path] = data

    def files(self, work_id, limit=100, offset=0):
        return [
            {"name": name.split("/")[1]} for name in self.objects if name.startswith(work_id + "/")
        ][offset : offset + limit]

    def download(self, path):
        if path not in self.objects:
            raise AppError(404, "not_found", "Not found")
        return self.objects[path]

    def signed_link(self, path, expires_in):
        return "https://example.supabase.co/storage/v1/object/sign/" + path + "?token=test"


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            supabase_url="https://example.supabase.co",
            service_key="test-only",
            users=frozenset({USER}),
            reviewers=frozenset({REVIEWER}),
            cors_origins=("http://localhost:5173",),
            max_upload_bytes=1024,
        )
        self.db = FakeGateway(self.settings)
        self.app = create_app(self.settings, self.db)
        self.client = client_for(self.app)
        self.user = {"Authorization": "Bearer user-token"}
        self.reviewer = {"Authorization": "Bearer reviewer-token"}

    def work(self):
        response = self.client.post(
            "/api/v1/work-items", json={"classification_reason": "initial"}, headers=self.user
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["work_id"]

    def save(self, work_id, lines=1):
        return self.client.put(
            f"/api/v1/work-items/{work_id}/request-fields",
            json={"rows": extracted(lines)},
            headers=self.user,
        )

    def decision(self, work_id, line=1, **kwargs):
        return self.client.post(
            f"/api/v1/work-items/{work_id}/decisions",
            headers=self.reviewer,
            json={
                "line_no": line,
                "summary": "Human decision",
                "comparison_confirmed": True,
                "comparison_note": "규격 단위 통화 비용 조건을 검토했습니다.",
            }
            | kwargs,
        )

    def test_health_and_readiness(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(client_for(create_app(Settings())).get("/ready").status_code, 503)

    def test_business_api_requires_login(self):
        self.assertEqual(self.client.get("/api/v1/work-items").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/v1/work-items", headers={"Authorization": "Bearer forged"}
            ).status_code,
            401,
        )

    def test_allowlist_and_reviewer_roles(self):
        self.assertEqual(
            self.client.get(
                "/api/v1/work-items", headers={"Authorization": "Bearer outsider-token"}
            ).status_code,
            403,
        )
        work_id = self.work()
        response = self.client.post(
            f"/api/v1/work-items/{work_id}/reject", json={"reason": "no"}, headers=self.user
        )
        self.assertEqual(response.status_code, 403)

    def test_empty_allowlist_denies_even_valid_login(self):
        settings = Settings(supabase_url="https://example.supabase.co", service_key="test-only")
        client = client_for(create_app(settings, self.db))
        self.assertEqual(client.get("/api/v1/work-items", headers=self.user).status_code, 503)

    def test_login_refresh_and_no_store(self):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": "user@example.com", "password": "test-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("test-password", response.text)
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/refresh", json={"refresh_token": "refresh-test"}
            ).status_code,
            200,
        )

    def test_validation_does_not_echo_secrets(self):
        response = self.client.post(
            "/api/v1/auth/login", json={"email": "bad", "password": {"secret": "NEVER-ECHO-THIS"}}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("NEVER-ECHO-THIS", response.text)
        self.assertNotIn('"input"', response.text)

    def test_login_rate_limit(self):
        for _ in range(10):
            self.client.post(
                "/api/v1/auth/login", json={"email": "user@example.com", "password": "x"}
            )
        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login", json={"email": "user@example.com", "password": "x"}
            ).status_code,
            429,
        )

    def test_create_list_detail_pagination(self):
        work_id = self.work()
        self.work()
        result = self.client.get("/api/v1/work-items?limit=1", headers=self.user).json()
        self.assertEqual(len(result["items"]), 1)
        self.assertTrue(result["has_more"])
        self.assertEqual(
            self.client.get(f"/api/v1/work-items/{work_id}", headers=self.user).json()["work"][
                "status"
            ],
            "ON_HOLD",
        )

    def test_invalid_paths_and_pagination(self):
        self.assertEqual(
            self.client.get("/api/v1/work-items/not-valid", headers=self.user).status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/v1/work-items?limit=1000", headers=self.user).status_code, 422
        )
        self.assertEqual(
            self.client.get("/api/v1/reference-data/work_items", headers=self.user).status_code, 422
        )

    def test_complete_and_duplicate_fields(self):
        work_id = self.work()
        self.assertEqual(self.save(work_id).status_code, 200)
        bad = extracted()
        bad[-1] = bad[0]
        response = self.client.put(
            f"/api/v1/work-items/{work_id}/request-fields", json={"rows": bad}, headers=self.user
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(self.db.tables["request_fields"]), 19)

    def test_multiline_decisions_and_audit(self):
        work_id = self.work()
        self.assertEqual(self.save(work_id, 2).status_code, 200)
        first = self.decision(work_id, 1)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["work"]["status"], "ON_HOLD")
        self.assertIn(REVIEWER, first.json()["review_results"][0]["summary"])
        self.assertEqual(self.save(work_id).status_code, 409)
        last = self.decision(work_id, 2)
        self.assertEqual(last.json()["work"]["status"], "APPROVED")
        self.assertEqual(self.decision(work_id, 2).status_code, 409)

    def test_decision_requires_explicit_confirmation(self):
        work_id = self.work()
        self.save(work_id)
        self.assertEqual(self.decision(work_id, comparison_confirmed=False).status_code, 422)
        self.assertEqual(self.decision(work_id, comparison_confirmed=1).status_code, 422)

    def test_selected_quote_must_match_spec(self):
        work_id = self.work()
        self.save(work_id)
        self.db.tables["price_references"] = [
            {
                "reference_id": "Q1",
                "reference_type": "QUOTE",
                "item_id": "I1",
                "unit_price": "100",
                "reference_date": "2020-01-01",
            }
        ]
        self.db.tables["items"] = [
            {
                "item_id": "I1",
                "item_name": "Different",
                "specification": "1 kg",
                "processing": "raw",
            }
        ]
        response = self.decision(work_id, reference_id="Q1")
        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.db.tables["review_results"])

    def test_rejection_keeps_classification_and_actor(self):
        work_id = self.work()
        response = self.client.post(
            f"/api/v1/work-items/{work_id}/reject",
            json={"reason": "Not a price request"},
            headers=self.reviewer,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("initial", response.json()["classification_reason"])
        self.assertIn(REVIEWER, response.json()["classification_reason"])

    def test_private_upload_and_analysis_pipeline(self):
        work_id = self.work()
        content = "품목명: Rice\n규격: 1 kg\n가공: raw\n변경 단가: 105\n".encode()
        response = self.client.post(
            f"/api/v1/work-items/{work_id}/files",
            files={"file": ("../../request.txt", content, "text/plain")},
            headers=self.user,
        )
        self.assertEqual(response.status_code, 201, response.text)
        file_id = response.json()["file_id"]
        self.assertEqual(response.json()["original_filename"], "request.txt")
        self.assertNotIn("..", next(iter(self.db.objects)))
        response = self.client.post(
            f"/api/v1/work-items/{work_id}/analyze", json={"file_ids": [file_id]}, headers=self.user
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["saved_fields"], 19)
        self.assertTrue(response.json()["fields"][0]["source_location"].startswith("storage://"))
        self.assertEqual(
            self.client.get(f"/api/v1/work-items/{work_id}/files", headers=self.user).json()[
                "items"
            ][0]["file_id"],
            file_id,
        )

    def test_file_types_limits_and_signed_url_expiry(self):
        work_id = self.work()
        path = f"/api/v1/work-items/{work_id}/files"
        self.assertEqual(
            self.client.post(path, files={"file": ("x.exe", b"x")}, headers=self.user).status_code,
            415,
        )
        self.assertEqual(
            self.client.post(
                path, files={"file": ("x.pdf", b"not a PDF")}, headers=self.user
            ).status_code,
            415,
        )
        self.assertEqual(
            self.client.post(
                path, files={"file": ("x.txt", b"x" * 1025)}, headers=self.user
            ).status_code,
            413,
        )
        file_id = "a" * 32 + ".txt"
        self.assertEqual(
            self.client.post(
                path + f"/{file_id}/signed-url", json={"expires_in": 999999}, headers=self.user
            ).status_code,
            422,
        )

    def test_missing_file_does_not_replace_fields(self):
        work_id = self.work()
        self.save(work_id)
        response = self.client.post(
            f"/api/v1/work-items/{work_id}/analyze",
            json={"file_ids": ["a" * 32 + ".txt"]},
            headers=self.user,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.db.tables["request_fields"]), 19)

    def test_cors_and_openapi(self):
        response = self.client.options(
            "/api/v1/work-items",
            headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"},
        )
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
        self.assertGreaterEqual(len(self.client.get("/openapi.json").json()["paths"]), 16)
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_chunked_body_limit_is_enforced(self):
        def body():
            yield b"x" * (1024 * 1024 + 2048)

        response = self.client.post(
            "/api/v1/auth/login", content=body(), headers={"Content-Type": "application/json"}
        )
        self.assertEqual(response.status_code, 413)

    def test_review_has_no_automatic_decision(self):
        work_id = self.work()
        self.save(work_id)
        result = self.client.get(f"/api/v1/work-items/{work_id}/review", headers=self.user)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["items"][0]["automatic_decision"])
        self.assertTrue(result.json()["items"][0]["issues"])

    def test_oversized_extracted_value_is_a_validation_error(self):
        settings = Settings(
            supabase_url="https://example.supabase.co",
            service_key="test-only",
            users=frozenset({USER}),
            max_upload_bytes=20000,
        )
        db = FakeGateway(settings)
        client = client_for(create_app(settings, db))
        work_id = client.post("/api/v1/work-items", json={}, headers=self.user).json()["work_id"]
        file_id = "a" * 32 + ".txt"
        db.objects[work_id + "/" + file_id] = ("item_name: " + "x" * 10001).encode()
        response = client.post(
            f"/api/v1/work-items/{work_id}/analyze", json={"file_ids": [file_id]}, headers=self.user
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(db.tables["request_fields"])


class ExtractionTests(unittest.TestCase):
    def test_multiline_headers_and_missing_values(self):
        text = "거래처: Supplier\n통화: KRW\n[품목 1]\n품목명: Rice\n규격: 1 kg\n[품목 2]\n품목명: Beans\n규격: 2 kg\n"
        rows, warnings = extract_fields([Document("storage://test", text)])
        self.assertEqual(len(rows), 38)
        for n in (1, 2):
            values = {r["field_name"]: r["raw_value"] for r in rows if r["line_no"] == n}
            self.assertEqual(values["supplier_name"], "Supplier")
            self.assertEqual(values["currency"], "KRW")
            self.assertIsNone(values["origin"])
        self.assertTrue(warnings)

    def test_ambiguous_or_unstructured_text_not_guessed(self):
        for text in ("Please increase the price", "품목명: Rice\n품목명: Beans"):
            with self.assertRaises(AppError):
                extract_fields([Document("test", text)])

    def test_email_body_and_html_do_not_execute_scripts(self):
        msg = EmailMessage()
        msg.set_content("<script>품목명: Bad</script><div>품목명: Rice</div>", subtype="html")
        docs = read_documents(msg.as_bytes(), "eml", "storage://mail")
        rows, _ = extract_fields(docs)
        self.assertEqual(
            next(r["raw_value"] for r in rows if r["field_name"] == "item_name"), "Rice"
        )
        self.assertNotIn("Bad", docs[0].text)

    def test_pdf_text_and_scanned_pdf(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=400, height=400)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 350 Td (item_name: Rice) Tj ET")
        page[NameObject("/Contents")] = stream
        out = BytesIO()
        writer.write(out)
        docs = read_documents(out.getvalue(), "pdf", "storage://pdf")
        self.assertIn("Rice", docs[0].text)
        self.assertIn("#page=1", docs[0].location)
        blank = PdfWriter()
        blank.add_blank_page(width=100, height=100)
        out = BytesIO()
        blank.write(out)
        with self.assertRaises(AppError) as caught:
            read_documents(out.getvalue(), "pdf", "test")
        self.assertEqual(caught.exception.code, "ocr_required")

    def test_model_rejects_boolean_line_number(self):
        rows = extracted()
        rows[0]["line_no"] = True
        with self.assertRaises(ValidationError):
            FieldsRequest(rows=rows)


class BusinessRulesTests(unittest.TestCase):
    def test_decimal_parsing_and_real_dates(self):
        self.assertEqual(number("1,000.25"), Decimal("1000.25"))
        for raw in ("NaN", "Infinity", "1e999", "1.2 USD", "9" * 1000):
            self.assertIsNone(number(raw))
        self.assertIsNone(iso_date("2026-02-30"))
        self.assertIsNone(iso_date("20261019"))

    def test_cost_normalization_uses_coverage_not_requested_quantity(self):
        terms = [
            {
                "cost_type": "TRANSPORT",
                "included_in_price": "FALSE",
                "payer": "BUYER",
                "basis": "TOTAL",
                "amount": "100",
                "coverage_quantity": "10",
            },
            {"cost_type": "OTHER_LOGISTICS", "included_in_price": "TRUE", "payer": "BUYER"},
        ]
        self.assertEqual(landed_price("0.10", terms)["landed_unit_price"], "10.10")
        terms[0]["coverage_quantity"] = None
        self.assertIsNone(landed_price("100", terms)["landed_unit_price"])
        self.assertIsNone(landed_price("100", [])["landed_unit_price"])

    def test_quote_validation_moq_expiry_lead_time_and_price(self):
        requested = {
            "item_name": "Rice",
            "specification": "1 kg",
            "processing": "raw",
            "requested_quantity": "10",
            "effective_date": "2026-10-19",
        }
        item = {"item_name": "Rice", "specification": "1 kg", "processing": "raw"}
        quote = {
            "reference_type": "QUOTE",
            "unit_price": "10",
            "reference_date": "2026-01-01",
            "valid_until": "2026-12-31",
            "moq": "5",
        }
        validate_quote(requested, quote, item, date(2026, 10, 19))
        for change in (
            {"moq": "20"},
            {"valid_until": "2026-01-01"},
            {"reference_type": "PURCHASE_HISTORY"},
            {"unit_price": None},
        ):
            with self.assertRaises(AppError):
                validate_quote(requested, quote | change, item, date(2026, 10, 19))
        with self.assertRaises(AppError):
            validate_quote(
                requested | {"requested_delivery_date": "2026-10-20"},
                quote | {"lead_days": 10},
                item,
                date(2026, 10, 19),
            )

    def test_out_of_period_cost_is_not_silently_added(self):
        terms = [
            {
                "cost_type": "TRANSPORT",
                "included_in_price": "FALSE",
                "payer": "BUYER",
                "basis": "PER_UNIT",
                "amount": "2",
                "coverage_from": "2027-01-01",
            },
            {"cost_type": "OTHER_LOGISTICS", "included_in_price": "TRUE", "payer": "SUPPLIER"},
        ]
        self.assertIsNone(landed_price("10", terms, date(2026, 10, 19))["landed_unit_price"])


class TransportTests(unittest.TestCase):
    def gateway(self, opener, key="sb_secret_fake"):
        return SupabaseGateway(
            Settings(supabase_url="https://example.supabase.co", service_key=key), opener=opener
        )

    def test_missing_configuration_fails_without_network(self):
        with self.assertRaises(AppError):
            SupabaseGateway(Settings()).rows("work_items")

    def test_new_secret_is_not_sent_as_jwt_and_user_token_is_separate(self):
        class Opener:
            requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                return BytesIO(b"[]")

        opener = Opener()
        gateway = self.gateway(opener)
        gateway.rows("work_items")
        self.assertIsNone(opener.requests[-1].get_header("Authorization"))
        gateway.user("user-token")
        self.assertEqual(opener.requests[-1].get_header("Authorization"), "Bearer user-token")
        gateway.rows("work_items")
        self.assertIsNone(opener.requests[-1].get_header("Authorization"))

    def test_postgres_error_is_mapped_without_leaking_sql(self):
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    400,
                    "error",
                    {},
                    BytesIO(b'{"code":"23514","message":"secret SQL details"}'),
                )

        with self.assertRaises(AppError) as caught:
            self.gateway(Opener()).rpc("record_decision", {})
        self.assertEqual(caught.exception.status, 409)
        self.assertNotIn("secret SQL", caught.exception.message)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")
        )

    def test_filter_literals_escape_quotes(self):
        self.assertEqual(eq('A"B\\C'), 'eq."A\\"B\\\\C"')

    def test_settings_never_repr_key(self):
        self.assertNotIn("super-secret-value", repr(Settings(service_key="super-secret-value")))

    def test_config_rejects_credential_url(self):
        with patch.dict("os.environ", {"SUPABASE_URL": "https://user:password@example.com"}):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
