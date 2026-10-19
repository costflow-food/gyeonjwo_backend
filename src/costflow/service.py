from datetime import datetime, timedelta, timezone
from pathlib import PurePath
import re
from uuid import uuid4
from pydantic import ValidationError

from .core import landed_price, line_values, validate_quote, iso_date
from .errors import AppError
from .extraction import extract_fields, read_documents
from .gateway import eq
from .models import FieldsRequest

KST = timezone(timedelta(hours=9))
TYPES = {"pdf": "application/pdf", "eml": "message/rfc822", "txt": "text/plain"}
FILE_PATTERN = re.compile(r"^[0-9a-f]{32}\.(pdf|eml|txt)$")


class CostFlowService:
    def __init__(self, gateway):
        self.db = gateway

    def work(self, work_id):
        rows = self.db.rows("work_items", filters={"work_id": eq(work_id)}, limit=1)
        if not rows:
            raise AppError(404, "work_not_found", "업무를 찾을 수 없습니다.")
        return rows[0]

    def held(self, work_id):
        work = self.work(work_id)
        if work["status"] != "ON_HOLD":
            raise AppError(409, "work_closed", "종료된 업무는 변경할 수 없습니다.")
        return work

    def related(self, table, work_id, maximum):
        result = []
        while True:
            page_limit = min(500, maximum + 1 - len(result))
            batch = self.db.rows(
                table,
                filters={"work_id": eq(work_id)},
                limit=page_limit,
                offset=len(result),
                order="line_no.asc" + (",field_name.asc" if table == "request_fields" else ""),
            )
            result.extend(batch)
            if len(result) > maximum:
                raise AppError(422, "too_many_rows", "업무의 데이터 한도를 초과했습니다.")
            if len(batch) < page_limit:
                break
        return result

    def detail(self, work_id):
        return {
            "work": self.work(work_id),
            "request_fields": self.related("request_fields", work_id, 19000),
            "review_results": self.related("review_results", work_id, 1000),
        }

    def create(self, reason):
        work_id = self.db.rpc("issue_work_id", {})
        if not isinstance(work_id, str) or not re.fullmatch(r"W-\d{8}-\d{3}", work_id):
            raise AppError(502, "invalid_id", "업무 ID 발급 응답이 올바르지 않습니다.")
        return self.db.insert_work(
            {"work_id": work_id, "classification_reason": reason, "status": "ON_HOLD"}
        )

    def save_fields(self, work_id, body: FieldsRequest):
        self.held(work_id)
        count = self.db.rpc(
            "save_request_fields",
            {"p_work_id": work_id, "p_rows": [row.model_dump() for row in body.rows]},
        )
        return {"saved_fields": count}

    def reject(self, work_id, reason, user_id):
        work = self.held(work_id)
        previous = work.get("classification_reason") or ""
        stamp = datetime.now(KST).isoformat(timespec="seconds")
        record = previous + f"\n[거절자 {user_id}; {stamp}] " + reason
        rows = self.db.reject(work_id, record.strip())
        if not rows:
            raise AppError(409, "work_changed", "업무 상태가 변경되었습니다. 새로 조회해 주세요.")
        return rows[0]

    def upload(self, work_id, file):
        self.held(work_id)
        if self.db.rows("review_results", filters={"work_id": eq(work_id)}, limit=1):
            raise AppError(
                409, "decisions_started", "결정이 시작된 업무에는 파일을 추가할 수 없습니다."
            )
        filename = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        extension = PurePath(filename).suffix.lower().lstrip(".")
        if extension not in TYPES:
            raise AppError(415, "file_type", "PDF, EML, UTF-8 TXT만 업로드할 수 있습니다.")
        data = file.file.read(self.db.settings.max_upload_bytes + 1)
        if not data:
            raise AppError(422, "empty_file", "빈 파일은 업로드할 수 없습니다.")
        if len(data) > self.db.settings.max_upload_bytes:
            raise AppError(413, "file_too_large", "파일이 업로드 한도를 초과했습니다.")
        if extension == "pdf" and b"%PDF-" not in data[:1024]:
            raise AppError(415, "invalid_pdf_header", "PDF 파일 형식이 올바르지 않습니다.")
        if extension == "txt":
            try:
                data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise AppError(415, "invalid_text", "TXT 파일은 UTF-8이어야 합니다.") from None
        file_id = uuid4().hex + "." + extension
        self.db.upload(work_id + "/" + file_id, data, TYPES[extension])
        return {
            "file_id": file_id,
            "original_filename": filename[:255],
            "size": len(data),
            "content_type": TYPES[extension],
        }

    def files(self, work_id, limit, offset):
        self.work(work_id)
        rows = self.db.files(work_id, limit, offset)
        return {
            "items": [
                {
                    "file_id": f["name"],
                    "created_at": f.get("created_at"),
                    "metadata": f.get("metadata"),
                }
                for f in rows
                if FILE_PATTERN.fullmatch(f.get("name", ""))
            ],
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) == limit,
        }

    def signed_link(self, work_id, file_id, expires_in):
        self.work(work_id)
        return {
            "url": self.db.signed_link(work_id + "/" + file_id, expires_in),
            "expires_in": expires_in,
        }

    def analyze(self, work_id, file_ids):
        self.held(work_id)
        if self.db.rows("review_results", filters={"work_id": eq(work_id)}, limit=1):
            raise AppError(409, "decisions_started", "결정 후에는 재분석할 수 없습니다.")
        documents = []
        total = 0
        for file_id in file_ids:
            path = work_id + "/" + file_id
            data = self.db.download(path)
            total += len(data)
            if total > self.db.settings.max_upload_bytes:
                raise AppError(
                    413, "analysis_too_large", "분석할 파일의 합계가 업로드 한도를 초과했습니다."
                )
            documents.extend(
                read_documents(
                    data, file_id.rsplit(".", 1)[1], f"storage://{self.db.settings.bucket}/{path}"
                )
            )
        if sum(len(doc.text) for doc in documents) > 200000:
            raise AppError(
                422, "analysis_too_large", "분석 텍스트 합계가 200,000자를 초과했습니다."
            )
        rows, warnings = extract_fields(documents)
        try:
            body = FieldsRequest.model_validate({"rows": rows})
        except ValidationError:
            raise AppError(
                422,
                "invalid_extraction",
                "추출된 값이 필드의 길이·형식 한도를 초과했습니다. 원문을 확인해 주세요.",
            ) from None
        result = self.save_fields(work_id, body)
        return result | {"lines": len(rows) // 19, "fields": rows, "warnings": warnings}

    def decide(self, work_id, body, principal):
        self.held(work_id)
        fields = self.related("request_fields", work_id, 19000)
        requested = line_values(fields).get(body.line_no)
        if requested is None:
            raise AppError(409, "line_not_found", "추출값이 없는 품목행입니다.")
        if body.reference_id:
            quotes = self.db.rows(
                "price_references", filters={"reference_id": eq(body.reference_id)}, limit=1
            )
            if not quotes:
                raise AppError(404, "reference_not_found", "가격 근거를 찾을 수 없습니다.")
            quote = quotes[0]
            items = self.db.rows("items", filters={"item_id": eq(quote["item_id"])}, limit=1)
            if not items:
                raise AppError(409, "item_not_found", "견적의 기준 품목이 없습니다.")
            validate_quote(requested, quote, items[0], datetime.now(KST).date())
        stamp = datetime.now(KST).isoformat(timespec="seconds")
        summary = (
            body.summary
            + f"\n[검토자 {principal.user_id}; {stamp}]"
            + "\n[규격·단위·통화·비용 비교 확인] "
            + body.comparison_note
        )
        self.db.rpc(
            "record_decision",
            {
                "p_work_id": work_id,
                "p_line_no": body.line_no,
                "p_reference_id": body.reference_id,
                "p_summary": summary,
            },
        )
        return self.detail(work_id)

    def review(self, work_id, offset=0, limit=25):
        work = self.work(work_id)
        all_lines = line_values(self.related("request_fields", work_id, 19000))
        cards = []
        today = datetime.now(KST).date()
        for line_no in sorted(all_lines)[offset : offset + limit]:
            requested = all_lines[line_no]
            filters = {
                key: eq(requested[key])
                for key in ("item_name", "specification", "processing")
                if requested.get(key)
            }
            issues = ["단위·통화·비용의 비교 가능성을 검토자가 확인해야 합니다."]
            if len(filters) < 3:
                items = []
                issues.append("품목명·규격·가공상태를 확인해야 기준자료를 조회할 수 있습니다.")
            else:
                for key in ("origin", "grade"):
                    if requested.get(key):
                        filters[key] = eq(requested[key])
                    else:
                        issues.append(f"{key} 미확정: 후보 간 차이를 확인해 주세요.")
                items = self.db.rows("items", filters=filters, limit=6)
            candidates = []
            contracts = []
            suppliers = (
                self.db.rows(
                    "suppliers", filters={"supplier_name": eq(requested["supplier_name"])}, limit=2
                )
                if requested.get("supplier_name")
                else []
            )
            effective = iso_date(requested.get("effective_date")) or today
            for item in items[:5]:
                refs = self.db.rows(
                    "price_references",
                    filters={"item_id": eq(item["item_id"]), "reference_type": "eq.QUOTE"},
                    limit=11,
                    order="reference_date.desc,reference_id.asc",
                )
                if len(refs) > 10:
                    issues.append("품목별 견적 후보를 최근 10개까지만 표시합니다.")
                ref_ids = ",".join(eq(ref["reference_id"])[3:] for ref in refs[:10])
                all_terms = (
                    self.db.rows(
                        "cost_terms",
                        filters={"owner_type": "eq.REFERENCE", "owner_id": "in.(" + ref_ids + ")"},
                        limit=20,
                    )
                    if ref_ids
                    else []
                )
                for ref in refs[:10]:
                    problems = []
                    try:
                        validate_quote(requested, ref, item, today)
                    except AppError as exc:
                        problems.append(exc.message)
                    terms = [term for term in all_terms if term["owner_id"] == ref["reference_id"]]
                    candidates.append(
                        {
                            "reference": ref,
                            "item": item,
                            "cost": landed_price(ref.get("unit_price"), terms, effective),
                            "blocking_issues": problems,
                        }
                    )
                if len(suppliers) == 1:
                    active = self.db.rows(
                        "contracts",
                        filters={
                            "supplier_id": eq(suppliers[0]["supplier_id"]),
                            "item_id": eq(item["item_id"]),
                            "valid_from": "lte." + effective.isoformat(),
                            "valid_to": "gte." + effective.isoformat(),
                        },
                        limit=1,
                    )
                    for contract in active:
                        terms = self.db.rows(
                            "cost_terms",
                            filters={
                                "owner_type": "eq.CONTRACT",
                                "owner_id": eq(contract["contract_id"]),
                            },
                            limit=2,
                        )
                        contracts.append(
                            {
                                "contract": contract,
                                "cost": landed_price(contract["unit_price"], terms, effective),
                            }
                        )
            if len(items) > 5:
                issues.append(
                    "기준 품목 후보를 5개까지만 표시합니다. 원산지와 등급을 확인해 주세요."
                )
            cards.append(
                {
                    "line_no": line_no,
                    "requested": requested,
                    "contracts": contracts,
                    "quote_candidates": candidates,
                    "issues": issues,
                    "automatic_decision": False,
                }
            )
        return {
            "work": work,
            "items": cards,
            "total_lines": len(all_lines),
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < len(all_lines),
        }
