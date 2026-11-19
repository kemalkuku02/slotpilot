import json
import os
from typing import Any, Protocol

import httpx


class LLMProvider(Protocol):
    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]: ...


class OpenAICompatibleProvider:
    """Chat Completions adapter for OpenAI-compatible APIs; only the adapter is provider-specific."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", timeout: float = 30):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls):
        key = os.getenv("LLM_API_KEY")
        if not key:
            return None
        return cls(key, os.getenv("LLM_MODEL", "gpt-4o-mini"), os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))

    def complete(self, messages, tools):
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.2},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]


class FakeLLM:
    """Deterministic test adapter. Responses are consumed in order."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.requests: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        if not self.responses:
            return {"role": "assistant", "content": "Сейчас не могу продолжить. Передал диалог администратору."}
        return self.responses.pop(0)


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "tool_calls": [{"id": "fake-call", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
