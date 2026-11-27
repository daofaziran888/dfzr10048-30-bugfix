"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    # 合入、确认、续办和领取等动作可能携带正文、幂等键或续办来源；
    # 未显式声明的字段也必须保留并传给领域服务，不能在路由层丢掉。
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] | None = None
    content: str | None = None
    正文: str | None = None
    idempotency_key: str | None = None
    continued_from: str | None = None
