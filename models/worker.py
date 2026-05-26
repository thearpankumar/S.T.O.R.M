from pydantic import BaseModel, Field
from datetime import datetime, timezone
from typing import Literal
from models.tools import Tool
from models.features import Feature


class WorkerState(BaseModel):
    domain: str
    subdomain: str
    subdomain_id: int = 0
    tools_enterprise: list[Tool] = []
    tools_opensource: list[Tool] = []
    features: list[Feature] = []
    sub_features: dict[str, list[str]] = {}
    current_step: Literal["m2", "m3", "m4", "m5"] = "m2"
    retry_count: int = 0

    def to_checkpoint(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_checkpoint(cls, json_str: str) -> "WorkerState":
        return cls.model_validate_json(json_str)


class WorkerEvent(BaseModel):
    subdomain: str
    event_type: Literal["started", "progress", "completed", "failed"]
    step: str
    message: str
    progress_pct: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QuotaState(BaseModel):
    tool_name: str
    quota_remaining: int
    exhausted: bool = False
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
