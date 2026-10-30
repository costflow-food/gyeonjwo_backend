"""Replaceable temporary transport interface; no provider SDK is installed here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ModelInfo:
    # This is adapter configuration, never a field claimed by generated text.
    mode: Literal["MOCK", "PROVIDER"]
    model_id: str


class DraftModel(Protocol):
    info: ModelInfo

    def generate(self, request: dict) -> str:
        """Return a single JSON string or raise on call failure.

        Future team adapters own provider-specific messages, credentials, timeout
        and retry policy. No provider implementation is supplied in this phase.
        """
        ...
