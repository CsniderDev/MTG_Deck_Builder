from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    # Which LLM backs deck generation: "gemini" or "openai". Only the matching API
    # key needs to be set; with no key the app falls back to heuristic builds.
    llm_provider: str = "gemini"

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    frontend_origin: str = "http://localhost:5173"

    scryfall_base_url: str = "https://api.scryfall.com"
    edhrec_base_url: str = "https://json.edhrec.com"

    http_timeout_seconds: float = 20.0

    # --- Abuse / cost protection -------------------------------------------------
    # Requests per IP allowed per day on the LLM-triggering build/revamp endpoints.
    daily_ip_limit: int = 50
    # Hard ceiling on actual LLM API calls per UTC day, shared across all callers.
    llm_daily_call_limit: int = 300
    # Consecutive LLM failures before the circuit breaker opens and calls are
    # short-circuited to the heuristic path for a cooldown period.
    llm_circuit_failure_threshold: int = 3
    llm_circuit_cooldown_seconds: float = 60.0
    # How long an identical build/revamp response is served from cache before a
    # repeat request is allowed to hit the LLM/Scryfall/EDHREC again.
    response_cache_ttl_seconds: float = 120.0
    # Cloudflare Turnstile secret key. Leave empty to disable bot verification
    # entirely (the default, safe for local development).
    turnstile_secret_key: str = ""
    # Set to true only when deployed behind a trusted reverse proxy/CDN (e.g.
    # Cloudflare, nginx) that overwrites X-Forwarded-For itself. If the app is
    # ever directly internet-facing, leave this false - X-Forwarded-For is a
    # client-supplied header and trusting it without a proxy in front lets
    # anyone spoof their rate-limit identity.
    trust_x_forwarded_for: bool = False
    # Optional webhook (e.g. a Slack incoming webhook URL) notified when the
    # daily LLM call budget crosses 80% and 100% usage. Empty disables it.
    alert_webhook_url: str = ""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    @property
    def frontend_origins(self) -> list[str]:
        """Parse the comma-separated FRONTEND_ORIGIN setting into a CORS origin list."""
        return [origin.strip() for origin in self.frontend_origin.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings loaded from environment variables."""
    return Settings()
