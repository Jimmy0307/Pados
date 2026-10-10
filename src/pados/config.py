# SPDX-License-Identifier: AGPL-3.0-only
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

OPERATIONAL_DATABASE_DEFAULT = "pados_local_runtime"
CORPUS_DATABASE_DEFAULT = "pados_corpus_runtime"
OPERATIONAL_REVISION_DEFAULT = "20261010_0018"
CORPUS_REVISION_DEFAULT = "20260730_0011"
EXCLUDED_REVISION_DEFAULT = "20260730_0012"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = Field(default="local", alias="PADOS_ENV")
    runtime_mode: str = Field(default="native", alias="PADOS_RUNTIME_MODE")
    log_level: str = Field(default="INFO", alias="PADOS_LOG_LEVEL")
    db_host: str = Field(default="127.0.0.1", alias="PADOS_DB_HOST")
    db_port: int = Field(default=5432, alias="PADOS_DB_PORT")
    db_name: str = Field(default="pados_p6_7_validation", alias="PADOS_DB_NAME")
    operational_database_name: str = Field(
        default=OPERATIONAL_DATABASE_DEFAULT,
        alias="PADOS_OPERATIONAL_DATABASE_NAME",
    )
    corpus_database_name: str = Field(
        default=CORPUS_DATABASE_DEFAULT,
        alias="PADOS_CORPUS_DATABASE_NAME",
    )
    operational_revision: str = Field(
        default=OPERATIONAL_REVISION_DEFAULT,
        alias="PADOS_OPERATIONAL_REVISION",
    )
    corpus_revision: str = Field(
        default=CORPUS_REVISION_DEFAULT,
        alias="PADOS_CORPUS_REVISION",
    )
    excluded_revision: str = Field(
        default=EXCLUDED_REVISION_DEFAULT,
        alias="PADOS_EXCLUDED_REVISION",
    )
    db_user: str = Field(default="pados", alias="PADOS_DB_USER")
    db_password: str = Field(default="pados_local_password", alias="PADOS_DB_PASSWORD")
    db_echo: bool = Field(default=False, alias="PADOS_DB_ECHO")
    api_host: str = Field(default="127.0.0.1", alias="PADOS_API_HOST")
    api_port: int = Field(default=8088, alias="PADOS_API_PORT")
    ollama_base_url: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_BASE_URL")
    chat_provider: str = Field(default="mock", alias="PADOS_CHAT_PROVIDER")
    chat_model: str = Field(default="", alias="PADOS_CHAT_MODEL")
    fast_provider: str = Field(default="", alias="PADOS_FAST_PROVIDER")
    fast_model: str = Field(default="", alias="PADOS_FAST_MODEL")
    embed_provider: str = Field(default="mock", alias="PADOS_EMBED_PROVIDER")
    embed_model: str = Field(default="", alias="PADOS_EMBED_MODEL")
    executor_provider: str = Field(default="", alias="PADOS_EXECUTOR_PROVIDER")
    executor_model: str = Field(default="", alias="PADOS_EXECUTOR_MODEL")
    local_llm_concurrency: int = Field(default=1, alias="PADOS_LOCAL_LLM_CONCURRENCY")
    deterministic_workers: int = Field(default=4, alias="PADOS_DETERMINISTIC_WORKERS")
    max_active_worktrees: int = Field(default=2, alias="PADOS_MAX_ACTIVE_WORKTREES")
    control_api_token_hash: str = Field(
        default="85B427ADA27FF59781973AB29E5F90991D5ABA85771991E0B2D4F9169AB08492",
        alias="PADOS_CONTROL_API_TOKEN_HASH",
    )
    control_request_max_bytes: int = Field(default=8192, alias="PADOS_CONTROL_REQUEST_MAX_BYTES")
    product_workspace_root: str = Field(default="", alias="PADOS_PRODUCT_WORKSPACE_ROOT")
    product_object_root: str = Field(default="", alias="PADOS_PRODUCT_OBJECT_ROOT")
    product_database_name: str = Field(default="", alias="PADOS_PRODUCT_DATABASE_NAME")
    product_loop_provider: str = Field(default="AUTO", alias="PADOS_PRODUCT_LOOP_PROVIDER")
    # Optional Enterprise Capability Pack manifest file; unset never affects Core readiness.
    enterprise_config: str = Field(default="", alias="PADOS_ENTERPRISE_CONFIG")
    # Optional Local Compute Node registry; unset never affects Core readiness.
    compute_nodes_config: str = Field(default="", alias="PADOS_COMPUTE_NODES_CONFIG")
    # Optional Remote Operations config; unset means remote mode is off.
    remote_config: str = Field(default="", alias="PADOS_REMOTE_CONFIG")
    # Optional connectivity probes/projection; unset never affects Core readiness.
    connectivity_config: str = Field(default="", alias="PADOS_CONNECTIVITY_CONFIG")
    # Optional Herdr telemetry adapter; unset never affects Core readiness (telemetry only, no authority).
    herdr_config: str = Field(default="", alias="PADOS_HERDR_CONFIG")
    # Optional machine inventory (references only, never passwords); unset never affects Core readiness.
    machines_config: str = Field(default="", alias="PADOS_MACHINES_CONFIG")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def safe_database_url(self) -> str:
        return f"postgresql+psycopg://{self.db_user}:***@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


PROOF_REVISION_HEAD = "p6_21p_0015"
# The P6.19B proof graph requires exactly this main schema inside the proof DB
# (migrations/p6_19b/env.py). It is fixed history and does not move with the
# operational lane, which continues past it (20260930_0014 memory authority).
PROOF_FORMAL_MAIN_HEADS = ("20260730_0011", "20260808_0013")


def lane_revisions_accepted(settings: Settings, lane: str, revisions: list[str]) -> bool:
    """Return whether a database lane's main ``alembic_version`` rows are an exact accepted set.

    P6.15 separation is an invariant: the operational lane holds exactly its own
    head, the Corpus lane exactly its own head, and the proof lane (a separate
    database) exactly the fixed P6.19B formal main schema its proof graph requires.
    The excluded revision is never accepted on any lane.
    """
    current = sorted(revisions)
    if settings.excluded_revision in current:
        return False
    if lane == "operational":
        return current == [settings.operational_revision]
    if lane == "corpus":
        return current == [settings.corpus_revision]
    if lane == "proof":
        return current == sorted(PROOF_FORMAL_MAIN_HEADS)
    return False


def lane_settings(settings: Settings, database: str) -> Settings:
    """Bind shared connection settings to one lane's database."""
    return settings.model_copy(update={"db_name": database})
