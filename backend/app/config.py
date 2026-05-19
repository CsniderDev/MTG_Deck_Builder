from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3-flash-preview"
    frontend_origin: str = "http://localhost:5173"

    scryfall_base_url: str = "https://api.scryfall.com"
    edhrec_base_url: str = "https://json.edhrec.com"

    http_timeout_seconds: float = 20.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings loaded from environment variables."""
    return Settings()
