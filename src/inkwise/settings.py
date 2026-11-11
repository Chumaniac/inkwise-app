"""Explicit server settings; no implicit cloud credentials or model fallback."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ORIGINAL_IMAGE_ENTRY_LIMIT = 4096


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INKWISE_", extra="ignore")

    database_url: str = "sqlite:///data/private/inkwise.sqlite3"
    data_dir: Path = Path("data/private")
    cache_min_free_bytes: int = Field(default=256 * 1024 * 1024, ge=0, le=1024**4)
    cache_job_max_bytes: int = Field(default=1024**3, ge=1024 * 1024, le=16 * 1024**3)
    image_store_max_bytes: int = Field(default=2 * 1024**3, ge=1, le=1024**4)
    image_store_max_files: int = Field(default=ORIGINAL_IMAGE_ENTRY_LIMIT, ge=1, le=ORIGINAL_IMAGE_ENTRY_LIMIT)
    rubric_dir: Path = Path("knowledge/releases")
    reference_pack: Path = Path("teaching/reference-pack.json")
    max_pending_per_tenant: int = Field(default=20, ge=1, le=500)
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    max_image_pixels: int = Field(default=20_000_000, ge=1, le=40_000_000)
    model_location: str = "auto"
    model_context_tokens: int = Field(default=65536,ge=4096,le=1048576)
    model_tokenizer_path: Path | None = None
    model_tokenizer_sha256: str = ""
    model_input_price_microusd_per_million: int | None = Field(default=None,ge=0,le=1000000000)
    model_output_price_microusd_per_million: int | None = Field(default=None,ge=0,le=1000000000)
    cloud_enabled: bool = False
    cloud_model_base_url: str = ""
    cloud_model_name: str = ""
    cloud_model_revision: str = ""
    cloud_model_api_key: SecretStr = SecretStr("")
    cloud_context_tokens: int = Field(default=131072,ge=4096,le=1048576)
    cloud_input_price_microusd_per_million: int | None = Field(default=None,ge=0,le=1000000000)
    cloud_output_price_microusd_per_million: int | None = Field(default=None,ge=0,le=1000000000)
    cloud_structured_output: str = "json_object"
    cloud_long_text_threshold: int = Field(default=10000,ge=1000,le=15000)
    max_cloud_cost_microusd: int = Field(default=0,ge=0,le=100000000)
    route_health_timeout_seconds: float = Field(default=2,gt=0,le=10)
    model_global_concurrency: int = Field(default=2,ge=1,le=5)
    model_base_url: str = ""
    model_name: str = ""
    model_revision: str = ""
    pipeline_revision: str = "inkwise-0.4-v1"
    evaluation_salt: SecretStr = SecretStr("")
    evolution_enabled: bool = False
    evolution_tenant: str = ""
    evolution_interval_seconds: int = Field(default=86400,ge=300,le=604800)
    evolution_min_cases: int = Field(default=20,ge=4,le=1000)
    evolution_min_qwk: float = Field(default=.8,ge=-1,le=1,allow_inf_nan=False)
    evolution_max_cases: int = Field(default=500,ge=10,le=5000)
    calibration_dir: Path = Path("data/private/calibration")
    model_structured_output: str = "json_object"
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: float = Field(default=45, gt=0, le=120)
    model_max_output_tokens: int = Field(default=1800, ge=128, le=4096)
    model_concurrency: int = Field(default=2, ge=1, le=5)
    model_calls_per_job: int = Field(default=12, ge=6, le=60)
    job_timeout_seconds: int = Field(default=180, ge=10, le=600)
    ocr_base_url: str = ""
    ocr_backend: str = "disabled"
    ocr_secondary_backend: str = "disabled"
    ocr_model_path: Path = Path("artifacts/models/trocr/eaacaf452b06415df8f10bb6fad3a4c11e609406")
    ocr_secondary_max_lines: int = Field(default=8,ge=1,le=16)
    ocr_max_lines: int = Field(default=60,ge=1,le=64)
    ocr_max_pages: int = Field(default=6,ge=1,le=10)
    ocr_document_max_bytes: int = Field(default=30*1024*1024,ge=1024,le=100*1024*1024)
    ocr_disagreement_threshold: float = Field(default=.15,ge=0,le=1)
    ocr_deskew: bool = False
    vision_executable: Path = Path("artifacts/inkwise-vision")
    ocr_timeout_seconds: float = Field(default=60, gt=0, le=180)
    neo4j_uri: str = ""
    neo4j_user: str = ""
    neo4j_password: SecretStr = SecretStr("")
    chroma_host: str = ""
    chroma_port: int = 8000
    chroma_ssl: bool = False
    embedding_base_url: str = ""
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = ""
    lexicon_model_revision: str = ""
    kafka_bootstrap_servers: str = ""
    kafka_topic: str = "inkwise.chunks.v1"
    kafka_enabled: bool = False
    served_frontend_dir: Path = Path("web")


def model_configured(settings):
    return bool((settings.model_base_url and settings.model_name) or
        (settings.cloud_enabled and settings.cloud_model_base_url and settings.cloud_model_name))
