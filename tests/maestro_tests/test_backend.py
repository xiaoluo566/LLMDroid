import sys
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.decision_backend import (  # noqa: E402
    OpenAICompatibleBackend,
    FakeDecisionBackend,
    ModelRequest,
    ResponseFormatError,
    parse_json_object,
)


class BackendTests(unittest.TestCase):
    def test_parse_json_object_accepts_object(self):
        self.assertEqual(parse_json_object('{"decision": "ACTION"}'), {"decision": "ACTION"})

    def test_parse_json_object_rejects_non_object(self):
        with self.assertRaises(ResponseFormatError):
            parse_json_object('["ACTION"]')

    def test_parse_fenced_json_object(self):
        self.assertEqual(parse_json_object('\x60\x60\x60json\n{"decision":"ACTION"}\n\x60\x60\x60'),
                         {"decision": "ACTION"})

    def test_fake_backend_records_request_without_network(self):
        backend = FakeDecisionBackend({"planner": {"subgoal": "click:save"}})
        response = backend.complete(
            ModelRequest(
                role="planner",
                request_id="request-1",
                payload={"goal_id": "goal-1"},
            )
        )
        self.assertEqual(response.data["subgoal"], "click:save")
        self.assertEqual(len(backend.requests), 1)

    def test_openai_backend_passes_timeout_and_returns_real_usage(self):
        from types import SimpleNamespace
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = OpenAICompatibleBackend(client, "model").complete(
            ModelRequest("planner", "req-1", {"goal": "目标"}, timeout_seconds=7))
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual((result.input_tokens, result.output_tokens), (12, 4))
        self.assertEqual(calls[0]["timeout"], 7)
        self.assertEqual(calls[0]["model"], "model")

    def test_missing_usage_is_not_reported_as_zero(self):
        from types import SimpleNamespace
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: response)))
        result = OpenAICompatibleBackend(client, "model").complete(
            ModelRequest("planner", "req-1", {}))
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)

    def test_invalid_model_json_keeps_provider_usage_on_error(self):
        from types import SimpleNamespace
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="无效"))],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: response)))
        with self.assertRaises(ResponseFormatError) as caught:
            OpenAICompatibleBackend(client, "model").complete(ModelRequest("planner", "req-1", {}))
        self.assertEqual((caught.exception.input_tokens, caught.exception.output_tokens), (12, 4))


if __name__ == "__main__":
    unittest.main()
