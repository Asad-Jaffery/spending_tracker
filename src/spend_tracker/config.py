"""Read local settings without sending credentials to the browser or store."""

import os
from pathlib import Path


def local_setting(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if value:
        return value
    path = Path(".env")
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        setting, separator, value = line.partition("=")
        if separator and setting.strip() == name:
            value = value.strip().strip("\"'").strip()
            return value or None
    return None


def jev_api_key() -> str | None:
    return local_setting("JEVMODEL_API_KEY")


def database_url() -> str | None:
    return local_setting("SPEND_TRACKER_DATABASE_URL")
