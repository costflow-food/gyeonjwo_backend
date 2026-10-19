from enum import StrEnum
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .core import FIELD_NAMES

WorkId = Annotated[str, Field(pattern=r"^W-[0-9]{8}-[0-9]{3}$")]
FileId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}\.(pdf|eml|txt)$")]
Nonblank = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkStatus(StrEnum):
    ON_HOLD = "ON_HOLD"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class WorkCreate(InputModel):
    classification_reason: Nonblank | None = None


class RejectRequest(InputModel):
    reason: Nonblank


class ExtractedField(InputModel):
    line_no: Annotated[int, Field(strict=True, ge=1, le=1000)]
    field_name: str
    raw_value: Annotated[str, Field(max_length=10000)] | None = None
    source_location: Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]

    @field_validator("field_name")
    @classmethod
    def known_field(cls, value: str) -> str:
        if value not in FIELD_NAMES:
            raise ValueError("Unknown CostFlow field")
        return value


class FieldsRequest(InputModel):
    rows: Annotated[list[ExtractedField], Field(min_length=19, max_length=19000)]

    @model_validator(mode="after")
    def complete_lines(self):
        seen: dict[int, set[str]] = {}
        for row in self.rows:
            group = seen.setdefault(row.line_no, set())
            if row.field_name in group:
                raise ValueError("Duplicate field on a line")
            group.add(row.field_name)
        if any(group != set(FIELD_NAMES) for group in seen.values()):
            raise ValueError("Each line must contain all 19 fields exactly once")
        return self


class AnalyzeRequest(InputModel):
    file_ids: Annotated[list[FileId], Field(min_length=1, max_length=10)]

    @field_validator("file_ids")
    @classmethod
    def unique_files(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("Duplicate files are not allowed")
        return values


class DecisionRequest(InputModel):
    line_no: Annotated[int, Field(strict=True, ge=1, le=1000)]
    reference_id: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")] | None = None
    summary: Nonblank
    comparison_confirmed: Literal[True]
    comparison_note: Annotated[str, Field(min_length=10, max_length=2000, pattern=r"\S")]

    @field_validator("comparison_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit boolean true is required")
        return value


class SignedLinkRequest(InputModel):
    expires_in: Annotated[int, Field(strict=True, ge=30, le=900)] = 300


class LoginRequest(InputModel):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: SecretStr

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            raise ValueError("Invalid email address")
        return value


class RefreshRequest(InputModel):
    refresh_token: SecretStr


class WorkOut(BaseModel):
    work_id: str
    classification_reason: str | None
    status: WorkStatus


class WorkPage(BaseModel):
    items: list[WorkOut]
    offset: int
    limit: int
    has_more: bool


class DecisionOut(BaseModel):
    work_id: str
    line_no: int
    reference_id: str | None
    summary: str


class WorkDetail(BaseModel):
    work: WorkOut
    request_fields: list["SavedField"]
    review_results: list[DecisionOut]


class SavedField(ExtractedField):
    work_id: str


WorkDetail.model_rebuild()


class UploadOut(BaseModel):
    file_id: str
    original_filename: str
    size: int
    content_type: str


class AnalyzeOut(BaseModel):
    saved_fields: int
    lines: int
    warnings: list[str]
    fields: list[ExtractedField]
