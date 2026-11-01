import sys
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.decision_backend import (  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
