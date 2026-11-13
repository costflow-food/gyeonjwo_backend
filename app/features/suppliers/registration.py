"""Request validation for manually registered suppliers."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

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
