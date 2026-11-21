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
        self.last_usage: dict[str, int] | None = None

    @classmethod
    def from_env(cls):
        key = os.getenv("LLM_API_KEY")
        if not key:
            return None
        return cls(key, os.getenv("LLM_MODEL", "gpt-4o-mini"), os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"))

    def complete(self, messages, tools):
        self.last_usage = None
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "temperature": 0.2},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            if (isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool)
                    and isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool)):
                self.last_usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                }
        return payload["choices"][0]["message"]


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
