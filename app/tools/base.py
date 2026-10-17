"""Tool registry.

A tool is a JSON-schema declaration plus an async callable. The registry builds
the OpenAI-shaped ``tools`` array the LLM client sends, and dispatches calls back
by name. Tools declare ``requires`` so a missing integration is reported to the
user as "not configured" rather than crashing the turn.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("rexeniot.tools")


class ToolError(RuntimeError):
    """Raised for a recoverable tool failure; the text is shown to the model."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    #: Human-readable prerequisites, e.g. "Home Assistant URL + token".
    requires: str = ""
    dangerous: bool = False
    tags: list[str] = field(default_factory=list)

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def add(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        *,
        requires: str = "",
        dangerous: bool = False,
        tags: list[str] | None = None,
    ) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
        def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    handler=fn,
                    requires=requires,
                    dangerous=dangerous,
                    tags=tags or [],
                )
            )
            return fn

        return decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def available(self, include_dangerous: bool = False) -> list[Tool]:
        return [t for t in self._tools.values() if include_dangerous or not t.dangerous]

    def schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        tools = self.all() if names is None else [self._tools[n] for n in names if n in self._tools]
        return [t.schema() for t in tools]

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "requires": t.requires,
                "dangerous": t.dangerous,
                "tags": t.tags,
            }
            for t in self._tools.values()
        ]

    async def call(self, name: str, arguments: dict[str, Any] | str) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool '{name}'.")
        if isinstance(arguments, str):
            import json

            try:
                arguments = json.loads(arguments or "{}")
            except Exception as exc:
                raise ToolError(f"Tool '{name}' received malformed arguments: {exc}") from exc
        if not isinstance(arguments, dict):
            raise ToolError(f"Tool '{name}' expects an object of arguments.")
        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return result
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"Tool '{name}' got bad arguments: {exc}") from exc
        except Exception as exc:
            log.warning("tool %s failed: %s", name, exc)
            raise ToolError(f"Tool '{name}' failed: {type(exc).__name__}: {exc}") from exc


registry = ToolRegistry()
