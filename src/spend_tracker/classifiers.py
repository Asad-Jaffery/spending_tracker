"""Small Jev client. The classification engine does not depend on HTTP details."""

import json
import math
import time
import urllib.error
import urllib.request
from typing import Protocol
from uuid import uuid4

from spend_tracker.domain import Category, Prediction

JEV_URL = "https://jevmodel.org/v1/systemone"
CATEGORY_CRITERIA = {
    Category.GROCERIES: "Supermarkets, food markets, and household groceries",
    Category.DINING: "Restaurants, cafes, coffee shops, and prepared meals",
    Category.TRANSPORTATION: "Fuel, transit, rideshare, parking, and vehicle costs",
    Category.HOUSING: "Rent, mortgage, and housing payments",
    Category.UTILITIES: "Electricity, water, gas, internet, and phone service",
    Category.SHOPPING: "Retail goods and online purchases",
    Category.ENTERTAINMENT: "Streaming, games, movies, events, and recreation",
    Category.TRAVEL: "Flights, hotels, and travel bookings",
    Category.HEALTH: "Medical, dental, pharmacy, and fitness costs",
    Category.INCOME: "Salary, wages, refunds, and other incoming money",
    Category.TRANSFERS: "Transfers between accounts or payments of card balances",
    Category.OTHER: "Anything that does not fit another category",
}


class MerchantClassifier(Protocol):
    def classify(self, merchant: str, categories: tuple[Category, ...]) -> Prediction | None: ...


class UnconfiguredJevClassifier:
    def classify(self, merchant: str, categories: tuple[Category, ...]) -> None:
        return None


class JevError(RuntimeError):
    pass


class JevClassifier:
    def __init__(self, api_key: str, *, url: str = JEV_URL, timeout: float = 15):
        if not api_key.strip():
            raise ValueError("Jev API key is empty")
        self.api_key = api_key
        self.url = url
        self.timeout = timeout

    def classify(self, merchant: str, categories: tuple[Category, ...]) -> Prediction:
        payload = {
            "model": "jev-latest",
            "state": f"Merchant name: {merchant}",
            "questions": {
                "category": {
                    "type": "choice",
                    "instructions": "Classify this merchant. Use Other if unclear.",
                    "criteria": {
                        category.value: CATEGORY_CRITERIA[category] for category in categories
                    },
                }
            },
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": str(uuid4()),
            },
            method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    result = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 502) and attempt < 2:
                    time.sleep(2**attempt)
                    continue
                raise JevError(f"Jev returned HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                raise JevError(f"Jev request failed: {type(exc).__name__}") from None
        try:
            answer = result["answers"]["category"]
            if answer["type"] != "choice":
                raise ValueError("Unexpected answer type")
            category = Category(answer["choice"])
            if category not in categories:
                raise ValueError("Unexpected category")
            confidence = answer.get("confidence")
            if confidence is not None:
                try:
                    confidence = float(confidence)
                except (TypeError, ValueError):
                    confidence = None
                if confidence is not None and (
                    not math.isfinite(confidence) or not 0 <= confidence <= 1
                ):
                    confidence = None
            return Prediction(category, confidence)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise JevError("Jev returned an invalid classification") from exc
