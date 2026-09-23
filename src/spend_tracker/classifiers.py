"""Jev integration seam. No assumed endpoint, SDK, or fabricated predictions."""

from typing import Protocol

from spend_tracker.domain import Category, Prediction


class MerchantClassifier(Protocol):
    def classify(self, merchant: str, categories: tuple[Category, ...]) -> Prediction | None:
        """Return a prediction, or None if unavailable. Set timeouts in the adapter."""
        ...


class UnconfiguredJevClassifier:
    def classify(self, merchant: str, categories: tuple[Category, ...]) -> None:
        return None
