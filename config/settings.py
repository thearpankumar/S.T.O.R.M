from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator, ValidationInfo
import warnings


class Settings(BaseSettings):
    aws_region: str = Field(default="us-east-1")
    bedrock_model_id: str = Field(default="deepseek.v3.2")

    tavily_api_key: str = Field(default="")
    brightdata_api_key: str = Field(default="")
    firecrawl_api_key: str = Field(default="")

    db_path: str = Field(default="data/agent.db")
    excel_output_path: str = Field(default="output/cybersec_matrix.xlsx")
    max_workers: int = Field(default=3, ge=1, le=5)

    # ── LLM concurrency ───────────────────────────────────────────────────────
    # Max simultaneous Bedrock calls within one pipeline run.
    llm_concurrency: int = Field(default=4, ge=1, le=16)

    # ── Agent output limits ───────────────────────────────────────────────────
    # Hard caps enforced AFTER the LLM responds — excess items are discarded.
    # If the LLM ignores the count instruction and returns 30 tools, we still
    # only keep max_enterprise_tools + max_opensource_tools.
    max_enterprise_tools: int = Field(default=10, ge=1, le=30)
    max_opensource_tools: int = Field(default=5,  ge=1, le=20)
    max_features: int = Field(default=8,  ge=3, le=15)
    max_subfeatures_per_feature: int = Field(default=6, ge=2, le=12)

    # ── m5 matrix chunking ────────────────────────────────────────────────────
    # Max subfeatures sent in a SINGLE matrix LLM call.
    # If a feature has more sub-features than this, they are split into
    # mini-batches of this size, each getting its own LLM call.
    # This ensures no single call overflows the token budget regardless
    # of how many tools or sub-features the subdomain ends up with.
    max_sf_batch_size: int = Field(default=8, ge=2, le=20)

    web_search_enabled: bool = Field(default=True)
    consensus_calls: int = Field(default=3, ge=1, le=5)

    log_dir: str = Field(default="logs")
    log_file: str = Field(default="cybersec_agent.log")
    log_level_console: str = Field(default="INFO")
    log_level_file: str = Field(default="DEBUG")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("tavily_api_key", "brightdata_api_key", "firecrawl_api_key")
    @classmethod
    def warn_empty_api_keys(cls, v: str, info: ValidationInfo) -> str:
        if not v:
            field_name = info.field_name
            warnings.warn(
                f"{field_name} is not set. Some features may be unavailable.",
                UserWarning,
                stacklevel=2
            )
        return v


settings = Settings()


