"""Wrap legacy Tool subclasses into LangChain StructuredTool instances."""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.tools import StructuredTool, ToolException
from pydantic import BaseModel, create_model

from Agent.router_agent.tools.base import Tool as LegacyTool
from Agent.router_agent.tools.base import ToolResult


def _json_type_to_python(schema: dict[str, Any]) -> Any:
    """Map a JSON Schema type to a Python type."""
    t = schema.get("type")
    if "enum" in schema:
        enum_values = schema["enum"]
        return Literal[tuple(enum_values)]  # type: ignore[valid-type]
    if t == "string":
        return str
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        return list
    if t == "object":
        return dict
    return Any


def _build_args_schema(tool: LegacyTool) -> type[BaseModel]:
    """Dynamically build a pydantic BaseModel from tool.parameters (JSON Schema)."""
    params = tool.parameters
    if not isinstance(params, dict):
        params = {}
    props = params.get("properties", {})
    required = set(params.get("required", []))
    field_defs: dict[str, tuple[Any, Any]] = {}
    for key, schema in props.items():
        python_type = _json_type_to_python(schema)
        default = ... if key in required else None
        field_defs[key] = (python_type, default)
    model_name = f"{tool.name}Args"
    return create_model(model_name, **field_defs)


def wrap_tool(tool: LegacyTool) -> StructuredTool:
    """Wrap a legacy Tool instance as a LangChain StructuredTool."""

    async def _coro(**kwargs: Any) -> str:
        result: ToolResult = await tool.execute(**kwargs)
        if result.success:
            return result.content
        raise ToolException(result.error or "tool failed")

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        coroutine=_coro,
        args_schema=_build_args_schema(tool),
    )


def wrap_tools(tools: list[LegacyTool]) -> list[StructuredTool]:
    """Convenience: wrap a list of legacy Tool instances."""
    return [wrap_tool(t) for t in tools]
