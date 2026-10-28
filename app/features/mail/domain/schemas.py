from typing import Literal
from pydantic import Field, field_validator
from app.shared.domain.schemas import StrictModel


class MailClassificationRequest(StrictModel):
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=12000)


    @field_validator('subject', 'body')
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError('Mail text cannot be whitespace only')
        return value


class Classification(StrictModel):
    status: Literal['TRUE', 'FALSE', 'PENDING']
    reason: str = Field(min_length=1, max_length=500)
