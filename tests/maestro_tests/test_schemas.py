import sys
from pathlib import Path
import unittest


PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "LLMDroid-Droidbot" / "droidbot"
sys.path.insert(0, str(PACKAGE_ROOT))

from maestro.schemas import (  # noqa: E402
    ActionKind,
    ActionProposal,
    DecisionKind,
    DecisionResult,
    PageSnapshot,
    WidgetSnapshot,
)


class SchemaTests(unittest.TestCase):
    def test_action_requires_exactly_one_input_source(self):
        with self.assertRaises(ValueError):
            ActionProposal(
                action_id="a-1",
                kind=ActionKind.TEXT,
                widget_id="title",
                source_state_version=1,
            )

        with self.assertRaises(ValueError):
            ActionProposal(
                action_id="a-1",
                kind=ActionKind.TEXT,
                widget_id="title",
                value="hello",
                value_ref="title_value",
                source_state_version=1,
            )

    def test_action_rejects_negative_page_version(self):
        with self.assertRaises(ValueError):
            ActionProposal(
                action_id="a-1",
                kind=ActionKind.CLICK,
                widget_id="save",
                source_state_version=-1,
            )

    def test_action_decision_must_carry_an_action(self):
        with self.assertRaises(ValueError):
            DecisionResult(kind=DecisionKind.ACTION, reason="missing action")

    def test_unknown_decision_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            DecisionResult(kind="UNKNOWN_DECISION", reason="无效")

    def test_snapshot_finds_widget_without_mutating_input(self):
        snapshot = PageSnapshot(
            run_id="run-1",
            state_version=2,
            state_id="state-2",
            widgets=(
                WidgetSnapshot(widget_id="save", widget_type="clickable", text="Save"),
            ),
        )

        self.assertEqual(snapshot.find_widget("save").text, "Save")
        with self.assertRaises(KeyError):
            snapshot.find_widget("missing")
        self.assertEqual(len(snapshot.widgets), 1)


if __name__ == "__main__":
    unittest.main()
