"""Saved card import configuration, independent of transaction imports."""

from uuid import uuid4

from spend_tracker.parsing import IMPORT_PRESETS, InputError, preset_settings


def import_profiles(state: dict) -> list[dict]:
    saved = state.get("import_profiles", {})
    profiles = []
    for key, preset in IMPORT_PRESETS.items():
        profile = saved.get(
            key,
            {
                "label": preset["label"],
                "source": preset["source"],
                "base_preset": key,
                "settings": {},
            },
        )
        profiles.append(profile_details(key, profile))
    profiles.extend(
        profile_details(key, profile) for key, profile in saved.items() if key not in IMPORT_PRESETS
    )
    return profiles


def profile_details(key: str, profile: dict) -> dict:
    base = profile["base_preset"]
    return {
        "id": key,
        **profile,
        "settings": preset_settings(base, profile["settings"]),
        "default_settings": preset_settings(base),
    }


def resolve_import_profile(state: dict, key: str, overrides: dict | None) -> tuple[str, dict]:
    profile = state.get("import_profiles", {}).get(key)
    if profile is None:
        return key, preset_settings(key, overrides)
    # Retain the original parser identity namespace for overlapping Robinhood exports.
    settings = {**profile["settings"], **(overrides or {})}
    return profile["base_preset"], preset_settings(profile["base_preset"], settings)


def save_import_profile(state: dict, payload: dict) -> dict:
    key = payload.get("id")
    profiles = state.setdefault("import_profiles", {})
    if key is not None and key not in IMPORT_PRESETS and key not in profiles:
        raise InputError("Unknown card import preset")
    if key in IMPORT_PRESETS:
        base = key
    elif key in profiles:
        base = profiles[key]["base_preset"]
    else:
        base = payload.get("base_preset", "custom")
    settings = preset_settings(base, payload["settings"])
    label = payload["label"].strip()
    source = payload["source"].strip().casefold()
    if not label or len(label) > 100:
        raise InputError("Preset name must contain 1–100 characters")
    if not source or len(source) > 100:
        raise InputError("Account name must contain 1–100 characters")
    key = key or f"card-{uuid4().hex}"
    profile = {"label": label, "source": source, "base_preset": base, "settings": settings}
    profiles[key] = profile
    return profile_details(key, profile)
