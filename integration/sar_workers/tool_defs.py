"""极简 @tool 装饰器 — 不依赖任何外部包。

提供 ToolDef 数据类和 @tool 装饰器，用于定义 SAR 工具函数。
自动生成 JSON Schema 用于 LLM function calling。
"""
from __future__ import annotations
import inspect
import typing
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolDef:
    """工具定义 — 包含名称、描述、参数 schema、执行函数。"""
    name: str
    description: str
    parameters: dict
    func: Callable
    _node: Any = field(default=None, repr=False)

    def bind(self, node: Any) -> ToolDef:
        return ToolDef(
            name=self.name, description=self.description,
            parameters=self.parameters, func=self.func, _node=node,
        )

    def to_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            }
        }

    async def execute(self, **kwargs) -> str:
        return await self.func(self._node, **kwargs)


def tool(name: str, description: str):
    def decorator(func):
        schema = _signature_to_schema(func)
        return ToolDef(name=name, description=description,
                       parameters=schema, func=func)
    return decorator


def _signature_to_schema(func: Callable) -> dict:
    sig = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = {}
    param_docs = _parse_docstring_args(func)
    properties, required = {}, []
    for param_name, param in sig.parameters.items():
        if param_name == "node":
            continue
        annotation = hints.get(param_name, str)
        prop = {"type": _python_type_to_json(annotation)}
        if param_name in param_docs:
            prop["description"] = param_docs[param_name]
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        properties[param_name] = prop
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _python_type_to_json(annotation) -> str:
    if annotation is str or annotation == "str":
        return "string"
    if annotation is int or annotation == "int":
        return "integer"
    if annotation is float or annotation == "float":
        return "number"
    if annotation is bool or annotation == "bool":
        return "boolean"
    return "string"


def _parse_docstring_args(func: Callable) -> dict:
    doc = inspect.getdoc(func) or ""
    result, in_args, current_param = {}, False, None
    for line in doc.split("\n"):
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if in_args:
            if stripped and not stripped[0].isspace() and ":" in stripped:
                name, desc = stripped.split(":", 1)
                current_param = name.strip()
                result[current_param] = desc.strip()
            elif stripped and current_param:
                result[current_param] += " " + stripped
            elif not stripped:
                in_args = False
    return result
