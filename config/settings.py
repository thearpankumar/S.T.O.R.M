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
    log_display_lines: int = Field(default=200, ge=50, le=1000)
    event_queue_maxsize: int = Field(default=1000, ge=100, le=10000)
    event_timeout: float = Field(default=0.5, ge=0.1, le=5.0)

    t2_excel_output_path: str = Field(default="output/technique2_domain_rankings.xlsx")
    t2_max_enterprise_tools: int = Field(default=15, ge=5, le=30)
    t2_max_opensource_tools: int = Field(default=5, ge=2, le=10)
    t2_max_features: int = Field(default=10, ge=5, le=15)
    t2_max_subfeatures_per_feature: int = Field(default=6, ge=2, le=10)
    t2_max_sf_batch_size: int = Field(default=8, ge=2, le=20)
    t2_enable_web_search_ranking: bool = Field(default=True)

    t2_weight_subdomain_presence: float = Field(default=0.40, ge=0.0, le=1.0)
    t2_weight_feature_coverage: float = Field(default=0.20, ge=0.0, le=1.0)
    t2_weight_market_presence: float = Field(default=0.20, ge=0.0, le=1.0)
    t2_weight_rank_distribution: float = Field(default=0.20, ge=0.0, le=1.0)

    # ── Technique 3 — Cross-domain tool classification ────────────────────────
    t3_excel_output_path: str = Field(default="output/technique3_tool_classification.xlsx")
    # Number of tools sent in a single NIST classification LLM call.
    # At 20 tools/call and 2,000 tools, that is ~100 parallel-batched calls.
    t3_nist_batch_size: int = Field(default=20, ge=5, le=50)
    # When True, use LLM to assign NIST functions; when False, use rule-based domain mapping only.
    t3_enable_nist_llm: bool = Field(default=True)

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


