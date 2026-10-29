from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_base_url: str = "mock"
    agent_api_key: str = ""
    agent_webhook_secret: str = Field(default="", min_length=0)
    demo_password: str = ""
    session_secret: str = ""
    database_path: str = "./dashboard.db"
    public_base_url: str = "http://localhost:8001"
    # Cookies are Secure by default; set to false only for plain-HTTP local development.
    cookie_secure: bool = True
    session_max_age_seconds: int = 8 * 3600
    login_attempts_per_minute: int = 5
    agent_timeout_seconds: float = 10.0
    # Mock agent: seconds between a simulated action and its webhook.
    mock_event_delay_seconds: float = 2.0

    @property
    def mock_mode(self) -> bool:
        return self.agent_base_url.strip().lower() == "mock"

    @property
    def webhook_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/webhooks/agent"

    def validate_for_startup(self) -> None:
        missing = [
            name
            for name, value in [
                ("DEMO_PASSWORD", self.demo_password),
                ("SESSION_SECRET", self.session_secret),
                ("AGENT_WEBHOOK_SECRET", self.agent_webhook_secret),
            ]
            if not value
        ]
        if not self.mock_mode and not self.agent_api_key:
            missing.append("AGENT_API_KEY")
        if missing:
            raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
        if len(self.session_secret) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 characters")
        if not self.mock_mode and not self.agent_base_url.startswith("https://"):
            # Plain HTTP is allowed only for the docker-compose network and local development.
            if not self.agent_base_url.startswith(("http://agent", "http://localhost", "http://127.0.0.1")):
                raise RuntimeError("AGENT_BASE_URL must use https:// (or be 'mock')")


@lru_cache
def get_settings() -> Settings:
    return Settings()
