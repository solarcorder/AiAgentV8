"""
Tool declarations — §17.6/§18.4.

The legacy system's tools took a single `request_json` string
discriminated by an `operation` field populated by `$fromAI`: nothing
validated before execution. The target shape is one typed function per
operation, each declaring its own Pydantic argument schema (enum-
constrained fields, unknown fields rejected), its read/write
classification, the capability required to invoke it, and its risk tier.

The executor (executor.py) reads these declaratively. The model is never
asked what tier something is, whether it needs approval, or what
capability it requires — §18.1's line, restated: the model recommends
which tool and what arguments; the application enforces everything about
whether it actually happens.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenancy.context import TenantContext


class RiskTier(str, Enum):
    """§18.4."""

    READ = "READ"  # no gate — tenant-scoped by construction
    LOW_WRITE = "LOW_WRITE"  # automatic, audited, reversible
    MEDIUM = "MEDIUM"  # auto only if org policy allows the specific template; else approval
    HIGH = "HIGH"  # human approval, always
    CRITICAL = "CRITICAL"  # human approval + role >= admin + explicit re-typed confirmation


class ToolFunction(Protocol):
    async def __call__(self, args: BaseModel, context: TenantContext, session: AsyncSession) -> Any: ...


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    name: str
    description: str
    args_schema: type[BaseModel]
    read_write: str  # "read" | "write"
    capability: str  # e.g. "domain.read", "approval.request"
    risk_tier: RiskTier
    fn: ToolFunction


_REGISTRY: dict[str, ToolDeclaration] = {}


def register_tool(decl: ToolDeclaration) -> None:
    if decl.name in _REGISTRY:
        raise ValueError(f"tool '{decl.name}' already registered")
    _REGISTRY[decl.name] = decl


def get_tool(name: str) -> ToolDeclaration | None:
    return _REGISTRY.get(name)


def all_tools() -> list[ToolDeclaration]:
    return list(_REGISTRY.values())
