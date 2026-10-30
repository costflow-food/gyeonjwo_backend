"""KAN-60: deterministic counter-offer tools; no model or mail calls."""

from .engine import calculate_counter_offer, validate_counter_offer
from .agent import run_counter_offer
from .drafting import run_counter_offer_with_model

__all__ = ["calculate_counter_offer", "validate_counter_offer", "run_counter_offer", "run_counter_offer_with_model"]
