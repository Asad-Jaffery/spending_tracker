"""FastAPI routes for the local dashboard, imports, and corrections."""

import base64
import binascii
import copy
import re
import threading
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from spend_tracker.analytics import (
    available_months,
    dashboard,
    spending_over_time,
    transactions_for_month,
)
from spend_tracker.classifiers import JevClassifier, UnconfiguredJevClassifier
from spend_tracker.config import jev_api_key
from spend_tracker.domain import Category
from spend_tracker.import_profiles import (
    import_profiles,
    resolve_import_profile,
    save_import_profile,
)
from spend_tracker.parsing import parse_csv, parse_json, preset_settings
from spend_tracker.services import (
    correct_category,
    import_transactions,
    retry_unclassified,
    set_duplicate_exclusion,
)
from spend_tracker.storage import empty_state
from spend_tracker.synthetic import seed_june, seed_may, seed_presentation_history

MAX_UPLOAD_BYTES = 5_000_000
MONTH_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


class TrackerApp:
    def __init__(self, store, *, seed_demo: bool = True):
        self.store = store
        self.lock = threading.RLock()
        self.synthetic_state = empty_state()
        seed_presentation_history(self.synthetic_state)
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
            profiles = import_profiles(state)
            demo_state = copy.deepcopy(self.synthetic_state) if month == "synthetic" else None
        months = available_months(state)
        selected = month or (months[0] if months else "2026-06")
        synthetic = selected == "synthetic"
        if not synthetic and not MONTH_PATTERN.fullmatch(selected):
            raise ValueError("Month must be YYYY-MM")
        if not synthetic:
            datetime.strptime(selected, "%Y-%m")
        else:
            state = demo_state
        calendar_month = "2026-06" if synthetic else selected
        overview = dashboard(state, calendar_month)
        overview["month"] = selected
        overview["available_months"] = [*months, "synthetic"]
        return {
            "dashboard": overview,
            "history": spending_over_time(state),
            "transactions": transactions_for_month(state, calendar_month),
            "merchants": sorted(
                state["merchant_mappings"].values(),
                key=lambda item: item["display_name"].casefold(),
            ),
            "import_runs": list(reversed(state["import_runs"][-10:])),
            "categories": [category.value for category in Category],
            "demo": synthetic
            or (
                bool(state["transactions"])
                and all(item["source"].startswith("synthetic-") for item in state["transactions"])
            ),
            "jev_configured": bool(jev_api_key()),
            "import_presets": profiles,
            "new_card_settings": preset_settings("custom"),
        }

    def save_import_settings(self, payload: dict) -> dict:
        with self.lock:
            state = self.store.load()
            profile = save_import_profile(state, payload)
            self.store.save(state)
        return profile

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
        import_preset = payload.get("import_preset")
        if import_preset is not None and not filename.lower().endswith(".csv"):
            raise ValueError("Card import presets require a CSV file")
        if payload.get("import_settings") is not None and import_preset is None:
            raise ValueError("Choose a card preset before supplying preset settings")
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
        with self.lock:
            state = self.store.load()
            base_preset, settings = (
                resolve_import_profile(state, import_preset, payload.get("import_settings"))
                if import_preset is not None
                else (None, None)
            )
            source_key = payload["source"].strip().casefold()
            custom_mapping = payload.get("column_mapping")
            if filename.lower().endswith(".csv"):
                parsed = parse_csv(
                    content,
                    date_format=payload.get("date_format", "iso"),
                    amount_convention=payload.get("amount_convention", "expenses_positive"),
                    column_mapping=(
                        custom_mapping
                        if custom_mapping is not None
                        else state["csv_column_mappings"].get(source_key)
                    ),
                    import_preset=base_preset,
                    import_settings=settings,
                )
            elif filename.lower().endswith(".json"):
                parsed = parse_json(content)
            else:
                raise ValueError("File must end in .csv or .json")
            key = jev_api_key()
            classifier = JevClassifier(key) if key else UnconfiguredJevClassifier()
            report = import_transactions(
                state,
                parsed,
                filename=filename,
                content=content,
                source=payload.get("source", ""),
                currency=payload.get("currency", "USD"),
                classifier=classifier,
            )
            if (
                filename.lower().endswith(".csv")
                and custom_mapping is not None
                and import_preset is None
            ):
                state["csv_column_mappings"][source_key] = custom_mapping
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
            demo = any(
                item["id"] == payload.get("transaction_id")
                for item in self.synthetic_state["transactions"]
            )
            state = self.synthetic_state if demo else self.store.load()
            count = correct_category(
                state,
                str(payload.get("transaction_id", "")),
                category,
                str(payload.get("scope", "transaction")),
            )
            if not demo:
                self.store.save(state)
        return {"updated_transactions": count, "scope": payload.get("scope", "transaction")}

    def exclude_duplicate(self, payload: dict) -> dict:
        with self.lock:
            state = self.store.load()
            set_duplicate_exclusion(
                state,
                str(payload.get("transaction_id", "")),
                bool(payload.get("excluded", True)),
            )
            self.store.save(state)
        return {"transaction_id": payload["transaction_id"], "excluded": payload["excluded"]}


class ImportRequest(BaseModel):
    filename: str
    content_base64: str
    source: str
    currency: str = "USD"
    date_format: str = "iso"
    amount_convention: str = "expenses_positive"
    column_mapping: dict[str, str] | None = None
    import_preset: str | None = None
    import_settings: dict | None = None


class CorrectionRequest(BaseModel):
    transaction_id: str
    category: str
    scope: str = "transaction"


class ImportSettingsRequest(BaseModel):
    id: str | None = None
    label: str
    source: str
    base_preset: str = "custom"
    settings: dict


class DuplicateExclusionRequest(BaseModel):
    transaction_id: str
    excluded: bool


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

    @api.post("/api/import-presets")
    def save_import_settings(payload: ImportSettingsRequest):
        try:
            return tracker.save_import_settings(payload.model_dump())
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/api/duplicate-exclusion")
    def duplicate_exclusion(payload: DuplicateExclusionRequest):
        try:
            return tracker.exclude_duplicate(payload.model_dump())
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
