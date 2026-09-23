from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr("sqlite:///./data/spend_tracker.db")
    jev_min_confidence: float = Field(default=0.8, ge=0, le=1)
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, ge=1)
    max_import_rows: int = Field(default=10_000, ge=1)
    default_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
