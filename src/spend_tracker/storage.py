"""Local JSON persistence. Replace this boundary when adding Supabase/Postgres."""

import json
import os
import tempfile
from pathlib import Path


def empty_state() -> dict:
    return {"version": 1, "transactions": [], "merchant_mappings": {}, "import_runs": []}


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
        ):
            raise ValueError("Unsupported or invalid data store")
        return data

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
