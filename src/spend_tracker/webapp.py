"""FastAPI routes for the local dashboard, imports, and corrections."""

import base64
import binascii
import re
import threading
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from spend_tracker.analytics import available_months, dashboard, transactions_for_month
from spend_tracker.classifiers import JevClassifier, UnconfiguredJevClassifier
from spend_tracker.config import jev_api_key
from spend_tracker.domain import Category
from spend_tracker.parsing import parse_csv, parse_json
from spend_tracker.services import correct_category, import_transactions, retry_unclassified
from spend_tracker.synthetic import seed_june, seed_may

MAX_UPLOAD_BYTES = 5_000_000
MONTH_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


class TrackerApp:
    def __init__(self, store, *, seed_demo: bool = True):
        self.store = store
        self.lock = threading.RLock()
        if seed_demo:
            with self.lock:
                state = self.store.load()
                if not state["transactions"]:
                    seed_june(state)
                    seed_may(state)
                    self.store.save(state)
                elif any(
                    item["source"] == "synthetic-june" for item in state["transactions"]
                ) and not any(item["date"].startswith("2026-05") for item in state["transactions"]):
                    seed_may(state)
                    self.store.save(state)

    def snapshot(self, month: str | None = None) -> dict:
        with self.lock:
            state = self.store.load()
        months = available_months(state)
        selected = month or (months[0] if months else "2026-06")
        if not MONTH_PATTERN.fullmatch(selected):
            raise ValueError("Month must be YYYY-MM")
        datetime.strptime(selected, "%Y-%m")
        return {
            "dashboard": dashboard(state, selected),
            "transactions": transactions_for_month(state, selected),
            "merchants": sorted(
                state["merchant_mappings"].values(),
                key=lambda item: item["display_name"].casefold(),
            ),
            "import_runs": list(reversed(state["import_runs"][-10:])),
            "categories": [category.value for category in Category],
            "demo": any(item["source"].startswith("synthetic-") for item in state["transactions"]),
            "jev_configured": bool(jev_api_key()),
        }

    def import_file(self, payload: dict) -> dict:
        filename = str(payload.get("filename", ""))
        if "/" in filename or "\\" in filename or not filename:
            raise ValueError("Choose a CSV or JSON file")
        try:
            content = base64.b64decode(payload["content_base64"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise ValueError("File content is missing or invalid") from exc
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError("File exceeds the 5 MB limit")
        if payload.get("date_format", "iso") not in ("iso", "us"):
            raise ValueError("Date format must be iso or us")
        if payload.get("amount_convention", "expenses_positive") not in (
            "expenses_positive",
            "expenses_negative",
        ):
            raise ValueError("Invalid amount convention")
        if not isinstance(payload.get("source"), str) or not isinstance(
            payload.get("currency", "USD"), str
        ):
            raise ValueError("Source and currency must be text")
        if filename.lower().endswith(".csv"):
            parsed = parse_csv(
                content,
                date_format=payload.get("date_format", "iso"),
                amount_convention=payload.get("amount_convention", "expenses_positive"),
            )
        elif filename.lower().endswith(".json"):
            parsed = parse_json(content)
        else:
            raise ValueError("File must end in .csv or .json")
        key = jev_api_key()
        classifier = JevClassifier(key) if key else UnconfiguredJevClassifier()
        with self.lock:
            state = self.store.load()
            report = import_transactions(
                state,
                parsed,
                filename=filename,
                content=content,
                source=payload.get("source", ""),
                currency=payload.get("currency", "USD"),
                classifier=classifier,
            )
            self.store.save(state)
        return report

    def retry_classification(self) -> dict:
        key = jev_api_key()
        if not key:
            raise ValueError("Set JEVMODEL_API_KEY in .env or your terminal before retrying")
        with self.lock:
            state = self.store.load()
            report = retry_unclassified(state, JevClassifier(key))
            self.store.save(state)
        return report

    def correct(self, payload: dict) -> dict:
        category = Category(payload.get("category"))
        with self.lock:
            state = self.store.load()
            count = correct_category(
                state,
                str(payload.get("transaction_id", "")),
                category,
                str(payload.get("scope", "transaction")),
            )
            self.store.save(state)
        return {"updated_transactions": count, "scope": payload.get("scope", "transaction")}


class ImportRequest(BaseModel):
    filename: str
    content_base64: str
    source: str
    currency: str = "USD"
    date_format: str = "iso"
    amount_convention: str = "expenses_positive"


class CorrectionRequest(BaseModel):
    transaction_id: str
    category: str
    scope: str = "transaction"


def create_app(store, *, seed_demo: bool = True) -> FastAPI:
    tracker = TrackerApp(store, seed_demo=seed_demo)
    api = FastAPI(title="Spend Tracker API", version="0.3.0")

    @api.get("/api/state")
    def state(month: str | None = None):
        try:
            return tracker.snapshot(month)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/import")
    def import_file(payload: ImportRequest):
        try:
            return tracker.import_file(payload.model_dump())
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/correct")
    def correct(payload: CorrectionRequest):
        try:
            return tracker.correct(payload.model_dump())
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/retry-classification")
    def retry_classification():
        try:
            return tracker.retry_classification()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # A built React bundle can be served by the same Python process. During
    # development Vite serves the frontend and proxies /api to this app.
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.joinpath("index.html").is_file():
        api.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @api.get("/", include_in_schema=False)
        def index():
            return FileResponse(dist / "index.html")

    return api


def serve(store, *, host: str = "127.0.0.1", port: int = 8765, seed_demo: bool = True) -> None:
    api = create_app(store, seed_demo=seed_demo)
    uvicorn.run(api, host=host, port=port)
