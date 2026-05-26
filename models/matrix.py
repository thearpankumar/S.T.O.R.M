from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from typing import Any


class ToolSupportRow(BaseModel):
    subdomain: str
    feature: str
    sub_feature: str
    tool_support: dict[str, str]

    model_config = ConfigDict(strict=True)

    @field_validator("tool_support", mode="before")
    @classmethod
    def normalize_support(cls, v: dict[str, Any]) -> dict[str, str]:
        mapping = {
            "yes": "✔",
            "supported": "✔",
            "true": "✔",
            "full": "✔",
            "✔": "✔",
            "no": "✘",
            "not supported": "✘",
            "false": "✘",
            "✘": "✘",
            "x": "✘",
            "partial": "Partial",
            "limited": "Partial",
            "addon": "Partial",
        }
        result = {}
        for tool, val in v.items():
            val_str = str(val).lower().strip() if val else "✘"
            result[tool.strip()] = mapping.get(val_str, "✘")
        return result

    @field_validator("sub_feature", mode="before")
    @classmethod
    def strip_sub_feature(cls, v: str) -> str:
        return v.strip() if v else v

    @field_validator("feature", mode="before")
    @classmethod
    def strip_feature(cls, v: str) -> str:
        return v.strip() if v else v


class MatrixBatch(BaseModel):
    subdomain: str
    tools_enterprise: list[str]
    tools_opensource: list[str]
    rows: list[ToolSupportRow]
