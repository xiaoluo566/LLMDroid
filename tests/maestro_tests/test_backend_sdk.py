import importlib.util
import os
import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.decision_backend import ModelRequest, OpenAICompatibleBackend


@unittest.skipUnless(importlib.util.find_spec("openai") and importlib.util.find_spec("httpx"),
                     "需要已有的 OpenAI SDK 和 httpx")
class SDKIntegrationTests(unittest.TestCase):
    def test_real_sdk_uses_transport_once_and_preserves_usage(self):
        import httpx
        from openai import OpenAI

        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "id": "local", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant",
                             "content": '{"current_subgoal":"click:save"}'},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            })

        http = httpx.Client(transport=httpx.MockTransport(handler))
        client = OpenAI(api_key=os.environ.get("MAESTRO_SDK_TEST_KEY", "local-placeholder"),
                        base_url="https://example.invalid/v1",
                        http_client=http, max_retries=2)
        try:
            backend = OpenAICompatibleBackend(client, "local-test")
            self.assertEqual(backend._client.max_retries, 0)
            response = backend.complete(ModelRequest("planner", "req-1", {}, 3))
            self.assertEqual(response.data["current_subgoal"], "click:save")
            self.assertEqual((response.input_tokens, response.output_tokens), (12, 4))
            self.assertEqual(len(requests), 1)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
