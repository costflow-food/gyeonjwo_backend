"""Request validation for manually registered suppliers."""
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.features.work_cases.domain.rfq import mailbox_address

Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
OptionalText = Annotated[str, StringConstraints(max_length=200)]


class SupplierRegistrationInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    name: Name
    contact_name: OptionalText | None = None
    contact_phone: OptionalText | None = None
    contact_email: Annotated[str, StringConstraints(max_length=254)] | None = None
    location: OptionalText | None = None
    item_names: list[Name] = Field(default_factory=list, max_length=50)

    @field_validator('contact_email')
    @classmethod
    def normalize_email(cls, value):
        if not value:
            return None
        address = mailbox_address(value)
        if not all(address.split('@')):
            raise ValueError('Invalid recipient')
        return address

    @field_validator('name', 'contact_name', 'contact_phone', 'location')
    @classmethod
    def reject_null_character(cls, value):
        if value and '\x00' in value:
            raise ValueError('Invalid text')
        return value

    @field_validator('item_names')
    @classmethod
    def validate_items(cls, values):
        if any('\x00' in value for value in values):
            raise ValueError('Invalid item name')
        return values


Decimal9 = Annotated[str, StringConstraints(pattern=r'^[0-9]{1,9}(\.[0-9]{1,6})?$')]


class SupplierItemUpdateInput(BaseModel):
    """supplier_item_id가 있으면 그 품목의 속성 수정, 없으면 새 품목 추가다."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    supplier_item_id: UUID | None = None
    item_name: Name | None = None
    specification: OptionalText | None = None
    unit: OptionalText | None = None
    units_per_box: Decimal9 | Literal[''] | None = None
    weight_per_box: Decimal9 | Literal[''] | None = None
    origin: OptionalText | None = None
    notes: Annotated[str, StringConstraints(max_length=1000)] | None = None
    certifications: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] | None = Field(
        default=None, max_length=20)

    @field_validator('item_name', 'specification', 'unit', 'origin', 'notes')
    @classmethod
    def reject_null_character(cls, value):
        if value and '\x00' in value:
            raise ValueError('Invalid text')
        return value

    @field_validator('units_per_box', 'weight_per_box')
    @classmethod
    def positive(cls, value):
        if value and Decimal(value) <= 0:
            raise ValueError('Invalid number')
        return value

    @field_validator('certifications')
    @classmethod
    def validate_certifications(cls, values):
        if values and any('\x00' in value for value in values):
            raise ValueError('Invalid certification')
        return values

    @model_validator(mode='after')
    def require_name_for_new_item(self):
        if self.supplier_item_id is None and not self.item_name:
            raise ValueError('Item name required')
        if 'item_name' in self.model_fields_set and self.item_name is None:
            raise ValueError('Item name required')
        return self


class SupplierUpdateInput(BaseModel):
    """보낸 키만 바꾼다. 키가 없으면 그대로 두고, null·빈 문자열이면 비운다(업체명 제외).
    items에 없는 기존 품목은 그대로 남는다(삭제하지 않는다)."""
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    name: Name | None = None
    contact_name: OptionalText | None = None
    contact_phone: OptionalText | None = None
    contact_email: Annotated[str, StringConstraints(max_length=254)] | None = None
    location: OptionalText | None = None
    items: list[SupplierItemUpdateInput] | None = Field(default=None, max_length=50)

    @field_validator('contact_email')
    @classmethod
    def normalize_email(cls, value):
        if not value:
            return None
        address = mailbox_address(value)
        if not all(address.split('@')):
            raise ValueError('Invalid recipient')
        return address

    @field_validator('name', 'contact_name', 'contact_phone', 'location')
    @classmethod
    def reject_null_character(cls, value):
        if value and '\x00' in value:
            raise ValueError('Invalid text')
        return value

    @model_validator(mode='after')
    def validate_update(self):
        if not self.model_fields_set:
            raise ValueError('Nothing to update')
        if 'name' in self.model_fields_set and self.name is None:
            raise ValueError('Name required')
        if 'items' in self.model_fields_set and self.items is None:
            raise ValueError('Items required')
        ids = [item.supplier_item_id for item in self.items or [] if item.supplier_item_id]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate item')
        return self

    def body(self):
        """DB 함수에 넘길 값. 보내지 않은 키는 넣지 않는다."""
        return self.model_dump(mode='json', exclude_unset=True)
