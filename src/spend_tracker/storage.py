"""Local JSON persistence and compatibility for older saved categories."""

import json
import os
import tempfile
from pathlib import Path

from spend_tracker.domain import Category, ClassificationSource

RENAMED_CATEGORIES = {
    "Dining": Category.FOOD_COFFEE.value,
    "Health": Category.HEALTH_PERSONAL_WELLNESS.value,
}
REMOVED_CATEGORIES = {"Housing", "Utilities", "Income", "Transfers"}
LEGACY_NON_SPENDING = {"Income", "Transfers"}


def migrate_categories(state: dict) -> dict:
    """Keep older saved data usable after the category list changes."""
    for item in state["transactions"]:
        old = item["category"]
        if old in RENAMED_CATEGORIES:
            item["category"] = RENAMED_CATEGORIES[old]
        elif old in REMOVED_CATEGORIES:
            item["category"] = Category.OTHER.value
            item["classification_source"] = ClassificationSource.UNCLASSIFIED.value
            item["needs_review"] = True
            if old in LEGACY_NON_SPENDING:
                item["excluded_from_spending"] = True
    for merchant, mapping in list(state["merchant_mappings"].items()):
        old = mapping["category"]
        if old in RENAMED_CATEGORIES:
            mapping["category"] = RENAMED_CATEGORIES[old]
        elif old in REMOVED_CATEGORIES:
            del state["merchant_mappings"][merchant]
    return state


def empty_state() -> dict:
    return {
        "version": 1,
        "transactions": [],
        "merchant_mappings": {},
        "import_runs": [],
        "csv_column_mappings": {},
        "import_profiles": {},
    }


class JsonStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        if not self.path.exists():
            return empty_state()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read data store: {self.path}") from exc
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("transactions"), list)
            or not isinstance(data.get("merchant_mappings"), dict)
            or not isinstance(data.get("import_runs"), list)
            or not isinstance(data.get("csv_column_mappings", {}), dict)
            or not isinstance(data.get("import_profiles", {}), dict)
        ):
            raise ValueError("Unsupported or invalid data store")
        data.setdefault("csv_column_mappings", {})
        data.setdefault("import_profiles", {})
        return migrate_categories(data)

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".spend-tracker-",
                suffix=".json",
                delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump(state, file, indent=2, ensure_ascii=False)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
