"""Connect MAESTRO decisions to the original DroidBot policy and EventLog."""
import json
import logging
import os
import shutil
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

from .android_arrival import anchor_problem, arrival_problem, candidate_origin
from .android_bridge import bind_event, read_candidate_pool, snapshot_from_device
from .android_oracle import AndroidOracle, ModelAndroidOracle, assess
from .cooperation import Feedback, collaborate_once, merge_assessment, refresh_observation
from .goal_agent import GoalObservation, ModelGoalOracle
from .goal_selection import (ModelGoalSelector, candidate_is_already_satisfied, deduplicate_candidates,
                             step_binding)
from .goal_oracle import (NO_GOAL_RULE, GoalAssessment, freeze_goal_contract,
                          evaluate_goal, apply_goal_assessment)
from .filter_workflow import (ModelFilterPlanner, append_filter_stage,
                              observe_filter_stage, assess_filter_goal)
from .visual_evidence import inspect_tutorial_overlay
from .model_data import ModelDataAgent
from .planner import FixedGoalPlanner
from .schemas import Budget, DecisionKind, ExecutionRecord, GoalStatus, PageSnapshot, WidgetSnapshot
from .trace import JsonlTrace


def observed_record(action_id, before, event_log, after):
    dispatched = event_log.dispatched
    status = ('NOT_DISPATCHED' if not dispatched else
              'EXECUTION_UNKNOWN' if after is None else 'OBSERVED')
    return ExecutionRecord(action_id, dispatched, before, after, status)


def verified_event_ref(output, event_log):
    event_file = output / 'events' / f'event_{event_log.tag}.json'
    try:
        data = json.loads(event_file.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict)
            or data.get('start_state') != event_log.from_state.state_str
            or data.get('stop_state') != event_log.to_state.state_str):
        return None
    return event_file.relative_to(output).as_posix()


class TracedBackend:
    def __init__(self, backend, trace):
        self.backend, self.trace = backend, trace

    def complete(self, request):
        role = {'goal_selection': 'Planner', 'planner_stage': 'Planner',
                'oracle': 'Oracle', 'goal_oracle': 'Oracle'}.get(request.role, 'Data')
        details = {key: request.payload[key] for key in
                   ('state_version', 'current_subgoal', 'constraints', 'allow_recapture')
                   if key in request.payload}
        if 'candidates' in request.payload:
            details['candidate_ids'] = [item['candidate_id'] for item in request.payload['candidates']]
            details['candidates'] = request.payload['candidates']
        self.trace.write(role, 'request', request_id=request.request_id, **details)
        try:
            response = self.backend.complete(request)
        except Exception as error:
            self.trace.write(role, 'request_failed', request_id=request.request_id,
                             error_type=type(error).__name__)
            raise
        self.trace.write(role, 'response', request_id=request.request_id, proposal=response.data,
                         input_tokens=response.input_tokens, output_tokens=response.output_tokens)
        return response


class AndroidRuntime:
    def __init__(self, app, backend):
        self.app = app
        self.output = Path(app.output_dir)
        self.run_id = uuid4().hex
        stream = JsonlTrace(self.output / 'maestro-trace.jsonl')
        self._stream = stream
        self.data = ModelDataAgent(TracedBackend(backend, self))
        self.planner = FixedGoalPlanner()
        self.oracle = ModelAndroidOracle(TracedBackend(backend, self))
        self.goal_agent = ModelGoalOracle(TracedBackend(backend, self))
        self.goal_observations = ()
        self.filter_planner = ModelFilterPlanner(TracedBackend(backend, self))
        self.observer = AndroidOracle()
        self.version = 0
        self.state = None
        self.pending = None
        self.feedback = None
        self.passed = []
        self.uncertain_write = False
        self.canary = os.environ.get('LLMDROID_MAESTRO_CANARY') == '1'
        self.max_canary_goals = int(os.environ.get('LLMDROID_MAESTRO_CANARY_GOALS', '1'))
        if not 1 <= self.max_canary_goals <= 5:
            raise ValueError('Canary goal limit must be between 1 and 5')
        self.canary_goals = 0
        self.utg = None
        self.candidate = None
        self._unavailable_targets = {}
        self.goal_contract = None
        self.filter_progress = None
        self.visual_evidence = None
        self.selector = ModelGoalSelector(
            TracedBackend(backend, self), selection_hint=(
                'For this connectivity canary prioritize a function on the current page that exercises '
                'text input. If none is available, choose a function leading to an editable form or search. '
                'Select only from the provided real candidates.'
                if self.canary else ''))
        self.canary_fired = self.canary_finished = False
        self.write('Runner', 'start', package=app.package_name, mode='real_android',
                   protocol='early_guidance_canary' if self.canary else 'original_low_growth')

    def early_guidance_ready(self, device_state):
        return self.canary and not self.canary_fired and self._belongs(device_state)

    def write(self, role, event, **details):
        state = self.state
        context = dict(run_id=self.run_id)
        if state is not None:
            context.update(goal_id=state.goal_id, step=state.current_subgoal,
                           budget=asdict(state.budget))
        self._stream.write(role, event, **context, **details)

    def select(self, utg, clusters=None, current_state=None):
        self.utg = utg
        pool = read_candidate_pool(self.run_id, utg, self.app.package_name, clusters)
        candidates = tuple(candidate for candidate in pool.candidates
                           if candidate.target_widget_id is not None)
        grounded_count = len(candidates)
        current = current_state if current_state is not None else getattr(utg, 'last_state', None)
        page = None
        if self._belongs(current):
            try:
                page = snapshot_from_device(self.run_id, self.version, current)
            except (OSError, ValueError) as error:
                self.write('Planner', 'selection_observation_unavailable', error_type=type(error).__name__)
        candidates, deferred = self._available_candidates(candidates, page)
        candidates, duplicates = deduplicate_candidates(candidates)
        deduplicated_count = len(candidates)
        if page is not None:
            satisfied = tuple(c for c in candidates if candidate_is_already_satisfied(c, page))
            for candidate in satisfied:
                self.write('Planner', 'goal_already_satisfied', candidate_id=candidate.candidate_id,
                           function=candidate.function, state_id=page.state_id,
                           target_widget_id=candidate.target_widget_id, counted_as_success=False)
            candidates = tuple(c for c in candidates if c not in satisfied)
        self.canary_fired = self.canary
        self.write('Policy', 'canary_trigger' if self.canary else 'low_growth',
                   candidate_count=len(candidates), grounded_candidate_count=grounded_count,
                   guidance_candidate_count=len(pool.functions),
                   deduplicated_candidate_count=deduplicated_count,
                   deferred_candidates=deferred,
                   duplicate_candidates=[{'discarded_candidate_id': discarded.candidate_id,
                                          'kept_candidate_id': kept.candidate_id}
                                         for discarded, kept in duplicates],
                   rejected_candidates=[asdict(item) for item in pool.rejected])
        if not candidates:
            self.write('Planner', 'selection_empty', reason=(
                'all_bound_candidates_already_satisfied' if deduplicated_count else
                'bound_candidates_deferred' if deferred else 'no_bound_function_candidates'))
            self.canary_finished = self.canary
            return None
        selector = self.selector
        if self.canary and getattr(utg, 'last_state', None) is not None:
            selector = ModelGoalSelector(self.selector.backend, selection_hint=(
                self.selector.selection_hint + f' Current UTG state ID: {utg.last_state.get_id()}.'))
        selected = selector.select(self.run_id, candidates, Budget(),
                                   f'{self.run_id}:{uuid4().hex}')
        if selected.selection is None and selected.validation_error:
            self.write('Planner', 'plan_rejected', request_id=selected.request_id,
                       reason=selected.validation_error)
            repair = ModelGoalSelector(selector.backend, selection_hint=(
                selector.selection_hint + ' Previous plan rejected: ' + selected.validation_error
                + '. Return a corrected plan using the original function target.'))
            selected = repair.select(self.run_id, candidates, selected.budget,
                                     f'{self.run_id}:{uuid4().hex}')
        if selected.selection is None:
            self.write('Planner', 'selection_rejected', reason=selected.error,
                       request_id=selected.request_id, validation_error=selected.validation_error)
            self.canary_finished = self.canary
            return None
        self.state = selected.create_state(self.version)
        self.candidate = selected.selection.candidate
        self.goal_contract = None
        self.filter_progress = None
        self.visual_evidence = None
        self.canary_goals += 1
        self.passed = []
        self.goal_observations = ()
        self.write('Planner', 'goal_selected', source=dict(self.state.goal_source),
                   steps=self.state.subgoals)
        return selected.selection.candidate

    def _available_candidates(self, candidates, page):
        ready, deferred = [], []
        for candidate in candidates:
            origin = candidate_origin(candidate)
            prior = self._unavailable_targets.get(origin)
            if prior is not None:
                reason, operations = prior
                if page is None or anchor_problem(candidate, page, operations):
                    deferred.append({'candidate_id': candidate.candidate_id, 'reason': reason})
                    continue
                self._unavailable_targets.pop(origin)
            ready.append(candidate)
        return tuple(ready), deferred

    def arrival_problem(self, device_state):
        if self.state is None or self.candidate is None:
            return 'goal_not_selected'
        if not self._belongs(device_state):
            return 'foreground_outside_test_app'
        try:
            page = snapshot_from_device(self.run_id, self.state.state_version, device_state)
        except (OSError, ValueError):
            return 'navigation_observation_unavailable'
        return arrival_problem(self.candidate, page, self.state.subgoals)

    def block_navigation(self, reason):
        if self.state is not None and self.candidate is not None and reason.startswith('goal_anchor_'):
            operations = tuple(step_binding(step) for step in self.state.subgoals)
            self._unavailable_targets[candidate_origin(self.candidate)] = (reason, operations)
        self.finish('BLOCKED', reason, mark_attempted=False)

    def finish(self, status, reason, *, mark_attempted=True):
        if status == 'PASS' and (self.state is None or self.state.status != GoalStatus.PASS):
            raise ValueError('Goal PASS requires a verified goal assessment')
        if self.state is not None:
            self.write('Graph', 'goal_finished', status=status, reason=reason)
            source = dict(self.state.goal_source)
            if self.utg is not None and mark_attempted:
                for cluster in self.utg.clusters:
                    if str(cluster.get_id()) == source.get('cluster'):
                        cluster.update_tested_function(source['function'])
                        self.write('Planner', 'function_attempted', cluster_id=source['cluster'],
                                   function=source['function'])
                        break
        self.state, self.pending, self.feedback = None, None, None
        self.candidate, self.goal_contract = None, None
        self.filter_progress, self.visual_evidence = None, None
        self.goal_observations = ()
        self.canary_finished = self.canary and self.canary_goals >= self.max_canary_goals
        if self.canary and not self.canary_finished:
            self.canary_fired = False

    def _belongs(self, device_state):
        return (device_state is not None
                and (device_state.foreground_activity or '').partition('/')[0] == self.app.package_name)

    def prepare(self, device_state):
        try:
            return self._prepare(device_state)
        except Exception as error:
            self._failed_stage('preparation', error)
            return None

    def _failed_stage(self, stage, error):
        if stage == 'execution callback':
            self.uncertain_write = True
        reason = f'{stage} failed: {type(error).__name__}'
        logging.getLogger('droidbot').error('MAESTRO %s', reason)
        try:
            self.finish('UNKNOWN', reason)
        except Exception as trace_error:
            logging.getLogger('droidbot').error('MAESTRO trace failed: %s', type(trace_error).__name__)
            self.state, self.pending, self.feedback = None, None, None

    def _prepare(self, device_state):
        if self.state is None:
            return None
        if not self._belongs(device_state):
            self.finish('UNKNOWN', 'Foreground activity is outside the tested app')
            return None
        snapshot = snapshot_from_device(self.run_id, self.state.state_version, device_state)
        if self.candidate is not None and candidate_is_already_satisfied(self.candidate, snapshot):
            self.write('Planner', 'goal_already_satisfied', candidate_id=self.candidate.candidate_id,
                       function=self.candidate.function, state_id=snapshot.state_id,
                       target_widget_id=self.candidate.target_widget_id, counted_as_success=False)
            self.finish('SKIPPED', 'The navigation goal is already satisfied; no repeated click')
            return None
        if self.candidate is not None and self.goal_contract is None:
            problem = arrival_problem(self.candidate, snapshot, self.state.subgoals)
            if problem:
                self.write('Planner', 'goal_preparation_blocked', reason=problem,
                           target_widget_id=self.candidate.target_widget_id,
                           observed_page=snapshot.state_id)
                self.block_navigation(problem)
                return None
            self.goal_contract = freeze_goal_contract(self.candidate, self.state.goal_id, snapshot)
            self.write('Oracle', 'goal_rule_frozen', contract=asdict(self.goal_contract))
        device = getattr(device_state, 'device', None)
        if device is not None and not getattr(getattr(device, 'droidbot_ime', None), 'connected', False):
            step = self.state.current_subgoal or self.state.subgoals[0]
            transport_constraint = (step, 'ascii_only', 'true')
            if transport_constraint not in self.state.input_constraints:
                self.state = self.state._updated(
                    input_constraints=self.state.input_constraints + (transport_constraint,))
        outcome = collaborate_once(self.state, snapshot, self.planner, self.data,
                                   feedback=self.feedback)
        self.feedback = None
        self.state = outcome.state
        if outcome.result.kind != DecisionKind.ACTION:
            self.finish('UNKNOWN', outcome.result.reason)
            return None
        action = outcome.result.action
        try:
            event = bind_event(action, device_state, snapshot, self.state)
            self.state = self.state._updated(budget=self.state.budget.consume_model_call())
            rule_proposal = self.oracle.propose_rule(action, self.state, snapshot)
            rule = rule_proposal.rule
        except Exception as error:
            self.finish('UNKNOWN', f'Action or Oracle rejected: {type(error).__name__}')
            return None
        self.pending = (event, action, rule)
        self.before = snapshot
        # InputManager consumes this callback before sending the event.
        self.write('Data', 'action_proposed', action=asdict(action))
        self.write('Oracle', 'rule_frozen', action_id=action.action_id, rule=asdict(rule))
        return event

    def preflight(self, event, fresh):
        if self.pending is None or event is not self.pending[0]:
            return True
        _, action, _ = self.pending
        if not self._belongs(fresh):
            return False
        try:
            # The binding checks page hash and target. Compare coordinates too.
            rebound = bind_event(action, fresh, self.before, self.state)
            return [view.get('bounds') for view in rebound.get_views()] == [
                view.get('bounds') for view in event.get_views()]
        except ValueError:
            return False

    def owns_event(self, event):
        return self.pending is not None and event is self.pending[0]

    def on_event_result(self, event, event_log):
        try:
            self._on_event_result(event, event_log)
        except Exception as error:
            self._failed_stage('execution callback', error)

    def _on_event_result(self, event, event_log):
        if self.pending is None or event is not self.pending[0]:
            return
        _, action, rule = self.pending
        self.pending = None
        if event_log is None:
            self.uncertain_write = True
            self.finish('UNKNOWN', 'InputManager interrupted; dispatch outcome uncertain')
            return
        device_state = event_log.to_state
        self.version += 1
        page = (snapshot_from_device(self.run_id, self.version, device_state)
                if event_log.dispatched and self._belongs(device_state) else None)
        record = observed_record(action.action_id, action.source_state_version, event_log,
                                 page.state_version if page else None)
        if record.execution_status == 'EXECUTION_UNKNOWN':
            self.uncertain_write = True
        self.state = self.state.apply_execution(record)
        evidence = verified_event_ref(self.output, event_log) if page else None
        self.write('InputManager', 'execution_record', record=asdict(record), evidence_ref=evidence)
        if not page or not evidence:
            self.finish('UNKNOWN', record.execution_status if not page else 'EventLog is missing')
            return
        native_event_ref = evidence
        evidence = self._save_observation(action, page, native_event_ref, device_state)
        if self.goal_contract is not None and self.goal_contract.kind == 'tutorial_open':
            page, evidence = self._tutorial_observation(action, page, evidence, event_log,
                                                       native_event_ref)
        assessment = assess(rule, self.state, page, evidence)
        if assessment.status == 'UNKNOWN':
            assessment = self._observe_again(action, rule, page, event_log.device, evidence)
        self.write('Oracle', 'step_assessed', assessment=asdict(assessment))
        self.state = merge_assessment(self.state, assessment)
        if assessment.status in {'PASS', 'FAIL'}:
            page, evidence = self._rated_observation(action, page, evidence, assessment)
            if self._supports_goal_agent():
                self._retain_goal_observation(action, record, page, evidence, native_event_ref, event_log)
        if assessment.status == 'PASS':
            self.passed.append(rule.kind)
            if self.state.current_subgoal is None:
                self.write('Graph', 'planned_steps_observed', observed_rules=tuple(self.passed))
                self._finish_goal(page, evidence, getattr(event_log, 'device', None))
        elif assessment.status == 'FAIL' and (self.state.current_subgoal or '').startswith('generate:'):
            self.feedback = Feedback(uuid4().hex, self.run_id, self.state.goal_id, action.action_id,
                                     self.state.state_version, 'DATA_FAILURE',
                                     'Observed input did not match the proposed value', (evidence,))
        else:
            self.finish(assessment.status, 'Rule evaluation did not establish step completion')

    def _rated_observation(self, action, page, evidence, step_assessment):
        # Supplemental observations may have superseded the first callback page.
        if step_assessment.state_version != page.state_version:
            evidence = step_assessment.evidence_refs[0]
            saved = json.loads((self.output / evidence).read_text(encoding='utf-8'))
            page = PageSnapshot(saved['run_id'], saved['state_version'], saved['state_id'],
                                tuple(WidgetSnapshot(**w) for w in saved['widgets']))
            if saved['action_id'] != action.action_id:
                raise ValueError('Goal observation does not match the completed action')
        return page, evidence

    def _supports_goal_agent(self):
        contract = self.goal_contract
        return bool(contract and (contract.kind == 'open_view' or
                    (contract.kind == 'unsupported' and contract.reason == NO_GOAL_RULE)))

    def _retain_goal_observation(self, action, record, page, evidence, event_ref, log):
        if log.from_state.state_str != self.before.state_id:
            self.write('Oracle', 'goal_observation_rejected', action_id=action.action_id,
                       reason='Native event start did not match the prepared page')
            return
        before = snapshot_from_device(self.run_id, record.before_state_version, log.from_state)
        before_ref = self._save_observation(action, before, event_ref, log.from_state,
                                            prefix='before')
        row = GoalObservation(self.run_id, self.state.goal_id, action, record,
                              before, page, before_ref, evidence, event_ref)
        self.goal_observations += (row,)

    def _finish_goal(self, page, evidence, device):
        if self.goal_contract is None:
            self.finish('UNKNOWN', 'No frozen goal-level contract')
            return
        if self.goal_contract.kind == 'apply_filter':
            self._advance_filter(page, evidence)
            return
        verdict = evaluate_goal(self.goal_contract, self.state, page, evidence,
                                visual_evidence=self.visual_evidence)
        source = 'deterministic'
        if verdict.status == 'UNKNOWN' and self._supports_goal_agent():
            verdict, source = self._ask_goal_agent(verdict, device)
        self.write('Oracle', 'goal_assessed', assessment=asdict(verdict), verdict_source=source)
        if verdict.status == 'PASS':
            self.state = apply_goal_assessment(self.state, verdict)
        elif verdict.status == 'FAIL':
            self.state = self.state.finish(GoalStatus.FAIL)
        self.finish(verdict.status, verdict.reason)

    def _ask_goal_agent(self, fallback, device):
        if tuple(row.action.action_id for row in self.goal_observations) != self.state.observed_action_ids:
            return (replace(fallback, reason='Goal observations do not cover the verified action history'),
                    'model_unavailable')
        try:
            allow_recapture = device is not None and self.state.budget.model_calls_left >= 2
            decision = self._request_goal_verdict(allow_recapture)
            if not decision.recapture:
                return decision.assessment, 'model'
            self.write('Oracle', 'goal_observation_requested', action_id=fallback.action_id,
                       reason=decision.assessment.reason)
            if not self._recapture_goal(device):
                return (replace(fallback, reason='Goal recapture did not return a page in the tested app'),
                        'model_unavailable')
            return self._request_goal_verdict(False).assessment, 'model'
        except Exception as error:
            self.write('Oracle', 'goal_verdict_rejected', error_type=type(error).__name__)
            latest = self.goal_observations[-1] if self.goal_observations else None
            refs = (latest.after_ref, latest.event_ref) if latest else fallback.evidence_refs
            return (GoalAssessment(self.run_id, self.state.goal_id, self.state.state_version,
                                   fallback.action_id, 'UNKNOWN',
                                   'Goal verdict was unavailable or its evidence was invalid', refs),
                    'model_unavailable')

    def _request_goal_verdict(self, allow_recapture):
        self.state = self.state._updated(budget=self.state.budget.consume_model_call())
        return self.goal_agent.assess(self.state, self.goal_observations,
                                      allow_recapture=allow_recapture)

    def _recapture_goal(self, device):
        latest = self.goal_observations[-1]
        fresh = device.get_current_state()
        if not self._belongs(fresh):
            return False
        self.version += 1
        page = snapshot_from_device(self.run_id, self.version, fresh)
        evidence = self._save_observation(latest.action, page, latest.event_ref, fresh)
        self.state = self.state._updated(state_version=page.state_version)
        self.goal_observations = self.goal_observations[:-1] + (
            replace(latest, after=page, after_ref=evidence),)
        self.write('Oracle', 'goal_observation_captured', action_id=latest.action.action_id,
                   state_version=page.state_version, evidence_ref=evidence)
        return True

    def _advance_filter(self, page, evidence):
        try:
            if self.filter_progress is None:
                self.state = self.state._updated(budget=self.state.budget.consume_model_call())
                self.filter_progress = self.filter_planner.choose(
                    self.state, page, self.candidate.target_widget_id, evidence)
                self.write('Planner', 'filter_condition_frozen', progress=asdict(self.filter_progress))
            else:
                self.filter_progress = observe_filter_stage(self.filter_progress, self.state,
                                                            page, evidence)
                self.write('Oracle', 'filter_phase_observed', progress=asdict(self.filter_progress))
            if self.filter_progress.phase == 'verified':
                verdict = assess_filter_goal(self.filter_progress, self.state, page, evidence)
                self.write('Oracle', 'goal_assessed', assessment=asdict(verdict))
                if verdict.status == 'PASS':
                    self.state = apply_goal_assessment(self.state, verdict)
                self.finish(verdict.status, verdict.reason)
            else:
                self.state = append_filter_stage(self.state, self.filter_progress, page)
                self.write('Planner', 'stage_planned', phase=self.filter_progress.phase,
                           steps=self.state.subgoals)
        except Exception as error:
            self.write('Planner', 'filter_stage_rejected', error_type=type(error).__name__,
                       reason=str(error) if isinstance(error, (ValueError, KeyError)) else 'Stage request failed')
            self.finish('UNKNOWN', 'Filter stage or persisted-condition evidence was not established')

    def _screenshot_ref(self, native):
        """Retain the capture before DroidBot moves or removes its temporary file."""
        path = getattr(native, 'screenshot_path', None)
        if not isinstance(path, str):
            return None
        try:
            resolved = Path(path).resolve()
            resolved.relative_to(self.output.resolve())
            if not resolved.is_file():
                return None
            folder = self.output / 'maestro-observations' / 'screenshots'
            folder.mkdir(parents=True, exist_ok=True)
            saved = folder / f'{uuid4().hex}{resolved.suffix}'
            shutil.copyfile(resolved, saved)
            return saved.relative_to(self.output).as_posix()
        except (OSError, ValueError):
            return None

    def _tutorial_observation(self, action, page, evidence, log, native_event_ref):
        before = self._screenshot_ref(log.from_state)
        after = self._screenshot_ref(log.to_state)
        if before is None or after is None:
            self.write('Oracle', 'visual_unavailable', action_id=action.action_id,
                       reason='Native before/after screenshots are unavailable')
            return page, evidence
        # A first frame may be mid-animation. At most two read-only recaptures
        # establish a stable pair; no device action or model call is repeated.
        for attempt in range(2):
            self.write('Oracle', 'evidence_requested', action_id=action.action_id,
                       requests=('Independently recapture the tutorial spotlight without sending an event',))
            self.write('Planner', 'observe_only', action_id=action.action_id,
                       reason='Confirm a stable tutorial guide', attempt=attempt + 1)
            fresh = log.device.get_current_state()
            confirmed = self._screenshot_ref(fresh)
            if not self._belongs(fresh) or confirmed is None:
                break
            self.version += 1
            page = snapshot_from_device(self.run_id, self.version, fresh)
            self.state = refresh_observation(self.state, page, action.action_id)
            confirmed_evidence = self._save_observation(action, page, native_event_ref, fresh)
            proof = f'maestro-observations/visual-{page.state_version}.json'
            refs = (evidence, confirmed_evidence, before, after, confirmed, proof)
            self.visual_evidence = inspect_tutorial_overlay(
                self.run_id, self.state.goal_id, action.action_id, page.state_version,
                self.output / before, self.output / after, self.output / confirmed, refs)
            (self.output / proof).write_text(json.dumps(asdict(self.visual_evidence),
                                                       ensure_ascii=False), encoding='utf-8')
            self.write('Oracle', 'visual_assessed', visual=asdict(self.visual_evidence))
            evidence = confirmed_evidence
            if self.visual_evidence.status == 'PASS':
                break
            after = confirmed
        return page, evidence

    def _save_observation(self, action, page, event_ref, native=None, prefix='page'):
        folder = self.output / 'maestro-observations'
        folder.mkdir(exist_ok=True)
        # A filesystem-safe name avoids colons in action IDs on Windows.
        file = folder / f'{prefix}-{page.state_version}.json'
        file.write_text(json.dumps(dict(action_id=action.action_id, event_log=event_ref,
                                        screenshot_ref=self._screenshot_ref(native),
                                        **asdict(page)), ensure_ascii=False), encoding='utf-8')
        return file.relative_to(self.output).as_posix()

    def _observe_again(self, action, rule, page, device, original_evidence):
        feedback = Feedback(uuid4().hex, self.run_id, self.state.goal_id, action.action_id,
                            self.state.state_version, 'ORACLE_UNCERTAIN', 'Need fresh UI evidence',
                            (original_evidence,))
        outcome = collaborate_once(self.state, page, self.planner, self.data,
                                   oracle=self.observer, feedback=feedback)
        if outcome.observation is None:
            return assess(rule, self.state, page, original_evidence)
        self.state = outcome.state
        self.write('Oracle', 'evidence_requested', action_id=action.action_id,
                   requests=outcome.observation.evidence_requests)
        self.write('Planner', 'observe_only', action_id=action.action_id,
                   observation=asdict(outcome.observation))
        fresh = device.get_current_state()
        if not self._belongs(fresh):
            return assess(rule, self.state, page, original_evidence)
        self.version += 1
        page = snapshot_from_device(self.run_id, self.version, fresh)
        self.state = refresh_observation(self.state, page, action.action_id)
        prior = json.loads((self.output / original_evidence).read_text(encoding='utf-8'))
        native_event_ref = prior['event_log']
        if not native_event_ref.startswith('events/') or '..' in Path(native_event_ref).parts:
            raise ValueError('Supplemental observation must retain its native event')
        evidence = self._save_observation(action, page, native_event_ref, fresh)
        return assess(rule, self.state, page, evidence)
