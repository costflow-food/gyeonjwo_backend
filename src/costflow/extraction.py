from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from io import BytesIO
import re

from .core import FIELD_NAMES
from .errors import AppError

LABELS = {
    "거래처": "supplier_name",
    "공급사": "supplier_name",
    "공급사명": "supplier_name",
    "품목": "item_name",
    "품목명": "item_name",
    "규격": "specification",
    "원산지": "origin",
    "등급": "grade",
    "가공": "processing",
    "가공상태": "processing",
    "기존단가": "old_unit_price",
    "변경단가": "new_unit_price",
    "신규단가": "new_unit_price",
    "단위": "unit",
    "통화": "currency",
    "적용일": "effective_date",
    "변경사유": "reason",
    "사유": "reason",
    "요청수량": "requested_quantity",
    "수량": "requested_quantity",
    "요청납기": "requested_delivery_date",
    "납기일": "requested_delivery_date",
    "운송비": "transport_amount",
    "기타물류비": "other_logistics_amount",
    "비용기준": "cost_basis",
    "비용부담자": "cost_payer",
    "단가포함여부": "cost_included_in_price",
} | {field: field for field in FIELD_NAMES}


@dataclass
class Document:
    location: str
    text: str


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"br", "p", "div", "tr"}:
            self.output.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr"}:
            self.output.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.output.append(data)


def pdf_documents(content: bytes, location: str) -> list[Document]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise AppError(422, "encrypted_pdf", "암호화된 PDF는 분석할 수 없습니다.")
        if len(reader.pages) > 100:
            raise AppError(422, "too_many_pages", "PDF는 100페이지까지 분석할 수 있습니다.")
        documents = []
        count = 0
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            count += len(text)
            if count > 200000:
                raise AppError(422, "text_too_large", "PDF 텍스트가 분석 한도를 초과했습니다.")
            documents.append(Document(f"{location}#page={i}", text))
        if not any(d.text.strip() for d in documents):
            raise AppError(
                422,
                "ocr_required",
                "스캔 PDF에는 OCR이 필요합니다. 현재는 텍스트 PDF를 지원합니다.",
            )
        return documents
    except AppError:
        raise
    except Exception:
        raise AppError(422, "invalid_pdf", "PDF를 읽을 수 없습니다.") from None


def read_documents(content: bytes, extension: str, location: str) -> list[Document]:
    if extension == "pdf":
        return pdf_documents(content, location)
    if extension == "txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AppError(422, "text_encoding", "TXT 파일은 UTF-8로 저장해 주세요.") from None
        if len(text) > 200000:
            raise AppError(422, "text_too_large", "텍스트 분석 한도를 초과했습니다.")
        return [Document(location, text)]
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
        body = message.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        if body and body.get_content_subtype() == "html":
            parser = HtmlText()
            parser.feed(text)
            text = "".join(parser.output)
        docs = [Document(location + "#body", text)]
        for i, attachment in enumerate(message.iter_attachments(), 1):
            if i > 10:
                raise AppError(422, "too_many_attachments", "이메일 첨부는 10개까지 지원합니다.")
            if attachment.get_content_type() == "application/pdf":
                raw = attachment.get_payload(decode=True) or b""
                if len(raw) > 20 * 1024 * 1024:
                    raise AppError(422, "attachment_too_large", "첨부 PDF가 20MB를 초과했습니다.")
                docs.extend(pdf_documents(raw, f"{location}#attachment={i}"))
        if sum(len(d.text) for d in docs) > 200000:
            raise AppError(422, "text_too_large", "이메일 텍스트 분석 한도를 초과했습니다.")
        return docs
    except AppError:
        raise
    except Exception:
        raise AppError(422, "invalid_email", "EML 이메일을 읽을 수 없습니다.") from None


def extract_fields(documents: list[Document]) -> tuple[list[dict], list[str]]:
    """Extract explicit labels only. Missing values are NULL, never inferred."""
    groups = []
    for document in documents:
        header = {}
        group = {}
        document_groups = []
        marked = False
        for line_no, raw in enumerate(document.text.splitlines(), 1):
            line = raw.strip()
            if re.fullmatch(r"(?:\[\s*)?(?:품목|item)\s*#?\s*\d+(?:\s*\])?\s*[:：]?", line, re.I):
                if group.get("item_name", (None,))[0]:
                    document_groups.append(group)
                elif group:
                    header.update(group)
                group = {}
                marked = True
                continue
            match = re.fullmatch(r"([^:：=]{1,50})\s*[:：=]\s*(.*)", line)
            if not match:
                continue
            label = match[1].strip().lower()
            key = LABELS.get(label) or LABELS.get(label.replace(" ", ""))
            if not key:
                continue
            value = match[2].strip() or None
            entry = (value, f"{document.location}:line={line_no}")
            if key in group:
                raise AppError(
                    422,
                    "ambiguous_field",
                    "같은 품목에 동일한 라벨이 반복됩니다. 품목 번호로 구분해 주세요.",
                )
            if not marked and "item_name" not in group and key != "item_name":
                header[key] = entry
            group[key] = entry
        if group:
            document_groups.append(group)
        # A preamble before [품목 1] is shared context, not an extra item.
        for values in document_groups:
            if values.get("item_name", (None,))[0]:
                groups.append((header | values, document.location))
    if not groups:
        raise AppError(
            422,
            "no_labeled_items",
            "품목명: 값 형식의 추출값을 찾지 못했습니다. 수동 입력 또는 라벨 양식을 사용해 주세요.",
        )
    if len(groups) > 1000:
        raise AppError(422, "too_many_lines", "품목행은 최대 1000개입니다.")
    rows = []
    for line_no, (values, location) in enumerate(groups, 1):
        for name in FIELD_NAMES:
            value, source = values.get(name, (None, location + "#not-found"))
            rows.append(
                {
                    "line_no": line_no,
                    "field_name": name,
                    "raw_value": value,
                    "source_location": source,
                }
            )
    return rows, [
        "라벨 기반 추출입니다. 원문과 추출값을 검토한 뒤 결정해 주세요.",
        "라벨이 없는 자유 서술·OCR·AI 추론은 수행하지 않았습니다.",
    ]
