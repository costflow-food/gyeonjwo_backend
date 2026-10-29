"""Validated, evidence-backed ① result, independent of a model or DB SDK."""
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal
import re
import unicodedata

from pydantic import Field, model_validator
from app.shared.domain.schemas import StrictModel

FIELDS = ('requester_name', 'item_name', 'specification', 'quantity', 'unit',
          'previous_unit_price', 'unit_price', 'effective_from')


class Evidence(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=300)
    excerpt: str = Field(min_length=1, max_length=1500)


class ExtractedValue(StrictModel):
    value: str | None = Field(default=None, max_length=1000)
    evidence: list[Evidence] = Field(default_factory=list, max_length=10)


class ExtractionResult(StrictModel):
    fields: dict[str, ExtractedValue]
    unknown_fields: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def supported_fields(self):
        if set(self.fields) - set(FIELDS) or set(self.unknown_fields) - set(FIELDS):
            raise ValueError('UNKNOWN_FIELD')
        for key in FIELDS:
            self.fields.setdefault(key, ExtractedValue())
        self.unknown_fields = [key for key in FIELDS if self.fields[key].value is None]
        return self

    def validated(self, evidence_texts):
        for name, field in self.fields.items():
            if field.value is None:
                continue
            if not field.value.strip() or not field.evidence:
                raise ValueError('MISSING_EVIDENCE')
            for ref in field.evidence:
                if ref.evidence_id not in evidence_texts or ref.excerpt not in evidence_texts[ref.evidence_id]:
                    raise ValueError('EVIDENCE_NOT_READ')
            joined = ' '.join(ref.excerpt for ref in field.evidence)
            if name in {'quantity', 'previous_unit_price', 'unit_price'}:
                if not re.fullmatch(r'\d+(?:\.\d{1,6})?', field.value):
                    raise ValueError('INVALID_NUMBER')
                try:
                    number = Decimal(field.value)
                    if number > Decimal('99999999999999.999999') or (name == 'quantity' and number <= 0):
                        raise ValueError('INVALID_NUMBER')
                except InvalidOperation:
                    raise ValueError('INVALID_NUMBER') from None
                literals = re.findall(r'(?<![\d.])\d+(?:\.\d+)?(?![\d.])', joined.replace(',', ''))
                if not any(Decimal(literal) == number for literal in literals):
                    raise ValueError('SOURCE_MISMATCH')
            elif name == 'effective_from':
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', field.value):
                    raise ValueError('INVALID_DATE')
                expected = date.fromisoformat(field.value)
                source_dates = re.findall(r'(\d{4})\s*[-/.년]\s*(\d{1,2})\s*[-/.월]\s*(\d{1,2})', joined)
                if not any((int(y), int(m), int(d)) == (expected.year, expected.month, expected.day)
                           for y, m, d in source_dates):
                    raise ValueError('SOURCE_MISMATCH')
            elif field.value not in joined:
                raise ValueError('SOURCE_MISMATCH')
        return self

    def stored_fields(self):
        values = {name: field.value for name, field in self.fields.items()}
        item = values['item_name']
        values['standardized_item_key'] = (
            re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', item)).strip().casefold() if item else None)
        return values
