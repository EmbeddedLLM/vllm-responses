from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from vtol.utils.cache import Cache

CURR_DIR = Path(__file__).resolve().parent


class EnvConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="vtol_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        cli_parse_args=False,
    )
    # Config
    # db_path: str = "postgresql+psycopg://vllm:vllm@pgbouncer:5432/vtol"
    db_path: str = "sqlite+aiosqlite:///vtol.db"
    log_dir: str = "logs"
    port: int = 5969
    host: str = "0.0.0.0"
    workers: int = 1  # The suggested number of workers is (2*CPU)+1
    max_concurrency: int = 300
    db_init: bool = False
    db_reset: bool = False
    cache_reset: bool = False
    log_timings: bool = False
    # Observability (metrics + tracing)
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    tracing_enabled: bool = False
    otel_service_name: str = "vtol"
    tracing_sample_ratio: float = 0.01
    # Tools
    code_interpreter_mode: Literal["spawn", "external", "disabled"] = "spawn"
    code_interpreter_port: int = 5970
    code_interpreter_workers: int = 0
    pyodide_cache_dir: str | None = None
    code_interpreter_dev_bun_fallback: bool = False
    # Upstream LLM API (OpenAI-compatible, e.g. vLLM server)
    llm_api_base: str = "http://localhost:8080/v1"
    # OpenTelemetry configs
    opentelemetry_host: str = "otel-collector"
    opentelemetry_port: int = 4317
    # Keys
    service_key: SecretStr = "vllm"
    service_key_alt: SecretStr = ""
    encryption_key: SecretStr = "vllm"
    openai_api_key: SecretStr = ""
    # Redis
    redis_host: str = "localhost"
    redis_port: int = 6379
    # Optional: ResponseStore hot cache (Redis). Disabled by default.
    response_store_cache: bool = False
    response_store_cache_ttl_seconds: int = 3600

    @model_validator(mode="after")
    def check_alternate_service_key(self) -> Self:
        if self.service_key_alt.get_secret_value().strip() == "":
            self.service_key_alt = self.service_key
        return self

    @property
    def db_dialect(self) -> Literal["sqlite", "postgresql"]:
        """
        Show the dialect that's in use based on the `db_path`.
        """
        if self.db_path.startswith("sqlite"):
            return "sqlite"
        elif self.db_path.startswith("postgresql"):
            return "postgresql"
        else:
            raise ValueError(f'`db_path` "{self.db_path}" has an invalid dialect.')

    @property
    def service_key_plain(self) -> str:
        return self.service_key.get_secret_value()

    @property
    def service_key_alt_plain(self) -> str:
        return self.service_key_alt.get_secret_value()

    @property
    def encryption_key_plain(self) -> str:
        return self.encryption_key.get_secret_value()

    @property
    def openai_api_key_plain(self) -> str:
        return self.openai_api_key.get_secret_value()


ENV_CONFIG = EnvConfig()
CACHE = Cache(
    redis_url=f"redis://{ENV_CONFIG.redis_host}:{ENV_CONFIG.redis_port}/1",
)
