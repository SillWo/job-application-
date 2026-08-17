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

    # All normal LLM roles use the cloud OpenAI-compatible API.
    # ModelGateway already supports provider="openai_compat".
    llm_provider: str = "openai_compat"

    # One shared cloud endpoint/key/model for:
    # - resume/profile import
    # - policy compilation/filtering
    # - cover letters
    # - vacancy relevance analysis
    openai_base_url: str = "http://127.0.0.1:8045/v1"
    openai_api_key: str = "sk-placeholder"
    openai_model: str = "gemini-3-flash"
    openai_timeout: float = 180.0

    # Relevance analysis still uses the dedicated adapter, but that adapter
    # now reads the SAME openai_* settings above.
    relevance_provider: str = "antigravity"

    # Legacy Ollama settings are intentionally retained only so that the old
    # fallback branch in gateway.py cannot crash if somebody explicitly
    # selects provider="ollama". They are NOT used by the supplied .env.
    # You can physically delete the Ollama branch later as a cleanup.
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "hf.co/unsloth/Qwen3.5-9B-GGUF:UD-Q6_K_XL"

    frontend_dist: Path = ROOT / "frontend" / "dist"
    browser_headless: bool = False
    pointer_overlay: bool = True


settings = Settings()
Path("data").mkdir(exist_ok=True)
