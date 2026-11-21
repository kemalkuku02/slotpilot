from app.agent.provider import OpenAICompatibleProvider
from evals.live import estimate_cost


def test_openai_provider_keeps_usage_separate_from_message_payload(monkeypatch):
    payloads = iter([
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"role": "assistant", "content": "next"}}]},
    ])

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return next(payloads)

    monkeypatch.setattr("app.agent.provider.httpx.post", lambda *args, **kwargs: Response())
    provider = OpenAICompatibleProvider("test-key", "test-model")

    first = provider.complete([], [])
    assert first == {"role": "assistant", "content": "ok"}
    assert provider.last_usage == {"prompt_tokens": 10, "completion_tokens": 5}

    second = provider.complete([], [])
    assert second == {"role": "assistant", "content": "next"}
    assert provider.last_usage is None


def test_live_eval_cost_uses_reported_tokens_and_configured_rates():
    calls = [{"prompt_tokens": 100, "completion_tokens": 20}]

    assert estimate_cost(calls, 2.0, 8.0) == 0.00036
    assert estimate_cost(calls, None, 8.0) is None
    assert estimate_cost([{"prompt_tokens": None, "completion_tokens": None}], 2.0, 8.0) is None
    assert estimate_cost([{"prompt_tokens": None, "completion_tokens": None}], 0.0, 0.0) == 0.0
