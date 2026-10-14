"""Read the local Jev credential without sending it to the browser or store."""

import os
from pathlib import Path


def jev_api_key() -> str | None:
    key = os.getenv("JEVMODEL_API_KEY", "").strip()
    if key:
        return key
    path = Path(".env")
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == "JEVMODEL_API_KEY":
            key = value.strip().strip("\"'").strip()
            return key or None
    return None
