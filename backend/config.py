from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="JAO_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{(ROOT / 'data' / 'orchestrator.db').as_posix()}"

    # Production AI uses the cloud OpenAI-compatible API. Mock is test-only.
    llm_provider: str = "openai_compat"

    # One shared cloud endpoint/key/model for:
    # - resume/profile import
    # - policy compilation/filtering
    # - cover letters
    # - vacancy relevance analysis
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout: float = 180.0

    frontend_dist: Path = ROOT / "frontend" / "dist"
    browser_headless: bool = False
    pointer_overlay: bool = True


settings = Settings()
Path("data").mkdir(exist_ok=True)
