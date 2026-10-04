import re
import unicodedata


def normalize_merchant(description: str) -> str:
    value = unicodedata.normalize("NFKC", description).casefold().strip()
    value = re.sub(r"^(?:sq\s*\*|tst\s*\*|paypal\s*\*)\s*", "", value)
    value = re.sub(r"^(?:pos purchase|debit card purchase|card purchase)\s+", "", value)
    value = re.sub(r"\s+(?:#\s*\d+|\d{3,})\s*$", "", value)
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"[_\s]+", " ", value).strip()
    if not value:
        raise ValueError("Description does not contain a merchant name")
    if len(value) > 500:
        raise ValueError("Normalized merchant name exceeds 500 characters")
    return value
