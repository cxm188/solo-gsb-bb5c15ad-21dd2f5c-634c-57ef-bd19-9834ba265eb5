"""请求 / 响应模型。"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, StringConstraints


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("name must not be blank")
    return value


# 单个显示名的统一约束：1–64 字符、非空白（注册 / 检查 / 批量接口共用）
NameStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64),
    AfterValidator(_not_blank),
]


class NameRequest(BaseModel):
    """注册 / 检查请求体。"""

    name: NameStr = Field(examples=["Аdmin"])


class ReservationRequest(BaseModel):
    """预占请求体：名称 + 有效期秒数（30–600）。"""

    name: NameStr = Field(examples=["Аdmin"])
    ttl_seconds: int = Field(
        ge=30,
        le=600,
        examples=[120],
        description="预占有效期（秒），范围 30–600",
    )


class BatchNameRequest(BaseModel):
    """批量注册请求体：1–50 个名称，全部通过才落库。"""

    names: list[NameStr] = Field(
        min_length=1,
        max_length=50,
        examples=[["paypal", "Alice"]],
    )


class ScriptDetail(BaseModel):
    script: str
    characters: list[str]
    code_points: list[str]


class ConflictRecord(BaseModel):
    id: int
    original_name: str
    normalized_name: str
    reason: Literal["normalized_match", "confusable"]
    # 冲突来源：已注册名称或未过期预占（历史调用方缺省视为已注册）
    source: Literal["registered", "reservation"] = "registered"
    # 仅当 source == "reservation" 时存在
    expires_at: str | None = None


class AnalysisResult(BaseModel):
    """名称分析结果（检查接口的响应，也是注册前的内部判定结果）。"""

    namespace: str
    original_name: str
    normalized_name: str
    scripts: list[str]
    script_details: list[ScriptDetail]
    restricted_scripts_used: list[str]
    mixed_script_violation: bool
    conflicts: list[ConflictRecord]


class NameRecord(BaseModel):
    id: int
    namespace: str
    original_name: str
    normalized_name: str
    created_at: str


class ReservationRecord(BaseModel):
    """预占成功响应：不可猜测 token 与过期时刻（UTC）。"""

    id: int
    namespace: str
    original_name: str
    normalized_name: str
    token: str
    expires_at: str
    created_at: str


class BatchItemRecord(NameRecord):
    """批量注册成功的单项记录：落库字段 + 原请求索引。"""

    index: int


class BatchRegisterResponse(BaseModel):
    """批量注册成功响应：items 按原请求顺序返回。"""

    namespace: str
    items: list[BatchItemRecord]


class ErrorDetail(BaseModel):
    error: str
    message: str
