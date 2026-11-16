import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot' / 'droidbot'))
from maestro.android_bridge import bind_event, snapshot_from_device
from maestro.android_oracle import propose_rule
from maestro.cooperation import collaborate_once
from maestro.data_agent import RuleBasedDataAgent
from maestro.decision_backend import FakeDecisionBackend
from maestro.goal_selection import GoalCandidate, ModelGoalSelector
from maestro.planner import FixedGoalPlanner
from maestro.model_data import ModelDataAgent
from maestro.schemas import ActionKind, ActionProposal, GoalSpec, Budget, WidgetSnapshot
from maestro.session import create_goal_state


class EnterActionTests(unittest.TestCase):
    def setUp(self):
        self.view = {'resource_id':'app:id/name', 'text':'Work', 'class':'android.widget.EditText',
                     'visible':True, 'enabled':True, 'editable':True, 'focused':True,
                     'bounds':[[0,0],[100,50]]}
        self.raw = SimpleNamespace(views=[self.view], foreground_activity='app/.Main',
                                   state_str='hash', get_id=lambda: 1)
        self.page = snapshot_from_device('r', 1, self.raw)
        self.widget = self.page.widgets[0].widget_id
        self.state = create_goal_state('r', GoalSpec('g', 'Submit name', (f'enter:{self.widget}',)),1)

    def test_enter_schema_rejects_text_payload(self):
        with self.assertRaises(ValueError):
            ActionProposal('a', ActionKind.ENTER, self.widget,1,value='hidden input')
        with self.assertRaises(ValueError):
            ActionProposal('a', ActionKind.ENTER, self.widget,1,value_ref='name')

    def test_enter_step_is_separate_action_and_native_key_event(self):
        outcome = collaborate_once(self.state,self.page,FixedGoalPlanner(),RuleBasedDataAgent())
        self.assertEqual(outcome.result.action.kind, ActionKind.ENTER)
        with patch('maestro.android_bridge._key_event_class', return_value=lambda name: SimpleNamespace(name=name)):
            event = bind_event(outcome.result.action,self.raw,self.page,outcome.state)
            self.assertEqual(event.name,'ENTER')
            self.assertEqual(propose_rule(outcome.result.action,outcome.state,self.page).kind,'ui_change')
            self.view['focused'] = False
            with self.assertRaises(ValueError):
                bind_event(outcome.result.action,self.raw,self.page,outcome.state)

    def test_planner_accepts_enter_on_input_and_rejects_labels(self):
        candidate = GoalCandidate('r','c','1','Submit name','states/1','cluster',self.page.widgets)
        backend = FakeDecisionBackend({'goal_selection':{'candidate_id':'c','subgoals':[f'enter:{self.widget}']}})
        selected = ModelGoalSelector(backend).select('r',(candidate,),Budget(),'choose')
        self.assertIsNotNone(selected.selection)
        self.assertIn('enter:widget',backend.requests[0].payload['instruction'])
        invalid = replace(candidate, widgets=(WidgetSnapshot(self.widget,'button'),))
        rejected = ModelGoalSelector(backend).select('r',(invalid,),Budget(),'choose-invalid')
        self.assertIsNone(rejected.selection)

    def test_model_data_enter_path_does_not_call_backend(self):
        backend = FakeDecisionBackend()
        outcome = collaborate_once(self.state,self.page,FixedGoalPlanner(),ModelDataAgent(backend))
        self.assertEqual(outcome.result.action.kind,ActionKind.ENTER)
        self.assertEqual(backend.requests,[])
        invalid_page=replace(self.page,widgets=(WidgetSnapshot(self.widget,'button'),))
        rejected=collaborate_once(self.state,invalid_page,FixedGoalPlanner(),ModelDataAgent(backend))
        self.assertEqual(rejected.result.kind.value,'STOP')

    def test_native_enter_event_uses_original_keyevent(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'LLMDroid-Droidbot'))
        try:
            from droidbot.input_event import KeyEvent
        except ImportError as error:
            self.skipTest(f'DroidBot dependency missing: {error.name}')
        finally:
            sys.path.pop(0)
        outcome = collaborate_once(self.state,self.page,FixedGoalPlanner(),RuleBasedDataAgent())
        with patch('maestro.android_bridge._key_event_class',return_value=KeyEvent):
            event=bind_event(outcome.result.action,self.raw,self.page,outcome.state)
            self.assertEqual(event.to_dict()['name'],'ENTER')

    def test_action_ids_are_distinct_between_goals(self):
        other = create_goal_state('r',GoalSpec('g2','Submit another',(f'enter:{self.widget}',)),1)
        first=collaborate_once(self.state,self.page,FixedGoalPlanner(),RuleBasedDataAgent())
        second=collaborate_once(other,self.page,FixedGoalPlanner(),RuleBasedDataAgent())
        self.assertNotEqual(first.result.action.action_id,second.result.action.action_id)
