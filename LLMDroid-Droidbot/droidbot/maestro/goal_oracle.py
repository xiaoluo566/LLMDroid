"""Goal-level UI postconditions frozen before dispatch, independent of step rules."""

import re
from dataclasses import dataclass, replace
from typing import Optional, Tuple

from .schemas import ActionKind, GoalStatus
from .visual_evidence import VisualEvidence


@dataclass(frozen=True)
class GoalContract:
    run_id: str
    goal_id: str
    candidate_id: str
    function: str
    before_version: int
    target_widget_id: Optional[str]
    resource_id: Optional[str]
    target_text: str
    widget_type: str
    kind: str
    reason: str
    before_titles: Tuple[Tuple[Optional[str], str], ...] = ()


@dataclass(frozen=True)
class GoalAssessment:
    run_id: str
    goal_id: str
    state_version: int
    action_id: Optional[str]
    status: str
    reason: str
    evidence_refs: Tuple[str, ...]


def freeze_goal_contract(candidate, goal_id, before):
    """Bind a supported function to its original observed control, never a new target."""
    if before.run_id != candidate.run_id or not isinstance(goal_id, str) or not goal_id.strip():
        raise ValueError('Goal contract requires the candidate run and a goal ID')
    target = None
    if candidate.target_widget_id is not None:
        target = before.find_widget(candidate.target_widget_id) if any(
            item.widget_id == candidate.target_widget_id for item in before.widgets) else None
        source = next((item for item in candidate.widgets
                       if item.widget_id == candidate.target_widget_id), None)
        if (target is None or source is None
                or (target.resource_id, target.text, target.widget_type)
                != (source.resource_id, source.text, source.widget_type)):
            raise ValueError('Goal target is missing or differs from the original function control')
    supported = re.match(
        r'^(navigate\b|go to\b|switch to\b|select\b|choose\b|进入|切换|选择)',
        candidate.function.strip(), re.IGNORECASE,
    )
    kind, reason = 'unsupported', 'No supported goal-level postcondition for this function'
    if supported and target is not None and target.widget_type in {'button', 'clickable'}:
        if target.selected or target.checked:
            reason = 'Target was already selected before the goal; no new selection can be established'
        else:
            kind, reason = 'target_selected', 'Require the original destination/filter control to become selected or checked; this verifies that selection, not downstream business effects'
    if target is not None and target.widget_type in {'button', 'clickable'}:
        kind, reason = _arrival_kind(candidate.function, target, before, kind, reason)
    return GoalContract(
        candidate.run_id, goal_id, candidate.candidate_id, candidate.function,
        before.state_version, candidate.target_widget_id,
        target.resource_id if target else None, target.text if target else '',
        target.widget_type if target else '', kind, reason,
        tuple((w.resource_id, _normalize(w.text)) for w in before.widgets if _is_title(w)),
    )


def _normalize(text):
    return ' '.join(text.casefold().split())


def _is_title(widget):
    resource = (widget.resource_id or '').partition(':id/')[2]
    return (widget.widget_type == 'label' and bool(widget.text.strip())
            and bool(re.search(r'(?:^|_)(?:title|toolbar_label|toolbar_title|header_title)$', resource)))


def _tutorial_titles(page):
    return tuple(w for w in page.widgets if _is_title(w)
                 and _normalize(w.text) in {'tutorial', 'tutorials', '教程', '入门教程'})


def _entry(widget):
    return (widget.widget_type in {'button', 'clickable'} and bool(widget.text.strip())
            and bool(re.search(r'list|item', widget.resource_id or '', re.I)))


def _arrival_kind(function, target, before, kind, reason):
    function = function.strip()
    tutorial_list = re.match(
        r'^(?:get(?:ting)? started\b|入门|(?:open|show) (?:tutorials\b|tutorial list\b)|教程列表)',
        function, re.I)
    localized_entry = (_normalize(target.text) in {'入门', 'get started', 'getting started', '教程', 'tutorials'}
                       and re.match(r'^(?:open|show|start)\b|^(?:打开|显示)', function, re.I))
    if re.match(r'^(?:start|open) onboarding\b', function, re.I) and not localized_entry:
        return 'unsupported', 'Onboarding aliases require an observed get-started/tutorial entry label'
    if tutorial_list or localized_entry:
        return 'tutorial_list', 'Require a new tutorial title and multiple named tutorial entries'
    if re.match(r'^(?:open|start|launch|show)\b.*\btutorial\b|^打开.*教程', function, re.I):
        if _tutorial_titles(before) and _entry(target):
            return 'tutorial_open', 'Require the bound tutorial entry click and a new stable spotlight guide'
        return 'unsupported', 'Tutorial opening requires an observed entry inside a tutorial list'
    if re.match(r'^(?:filter (?:records|log)\b|过滤记录|筛选记录)', function, re.I):
        return 'apply_filter', 'Require a new condition, application and persisted-condition readback'
    if re.match(r'^(?:open|show|view)\b|^(?:打开|显示)', function, re.I) and target.text.strip():
        return 'open_view', 'Require a new destination title matching the original control label'
    return kind, reason


def _bound_observation(contract, state, after, refs):
    action = state.last_action
    source = dict(state.goal_source)
    return bool(
        refs and after is not None
        and state.run_id == contract.run_id == after.run_id
        and state.goal_id == contract.goal_id and state.goal_description == contract.function
        and (not source or source.get('candidate_id') == contract.candidate_id)
        and ('function' not in source or source['function'] == contract.function)
        and state.pending_action_id is None and action is not None
        and action.kind == ActionKind.CLICK and action.widget_id == contract.target_widget_id
        and action.action_id in state.observed_action_ids
        and state.completed_action_ids and state.completed_action_ids[-1] == action.action_id
        and action.source_state_version >= contract.before_version
        and after.state_version == state.state_version > action.source_state_version
        and state.status in {GoalStatus.ACTIVE, GoalStatus.UNKNOWN}
    )


def _selected_target(contract, after):
    matches = tuple(widget for widget in after.widgets
                    if widget.widget_id == contract.target_widget_id)
    if len(matches) != 1:
        return False
    target = matches[0]
    return (target.widget_type == contract.widget_type and target.text == contract.target_text
            and target.resource_id == contract.resource_id
            and (target.selected is True or target.checked is True))


def _new_title(contract, widget):
    return (_is_title(widget) and widget.widget_id != contract.target_widget_id
            and _normalize(widget.text) not in {text for _, text in contract.before_titles})


def _arrived(contract, after):
    if contract.kind == 'open_view':
        return any(_new_title(contract, w) and _normalize(w.text) == _normalize(contract.target_text)
                   for w in after.widgets)
    if contract.kind == 'tutorial_list':
        titles = tuple(w for w in _tutorial_titles(after) if _new_title(contract, w))
        entries = {(_normalize(w.text), w.widget_id) for w in after.widgets if _entry(w)}
        return bool(titles) and len({text for text, _ in entries}) >= 2
    return False


def _visual_matches(evidence, state):
    return (isinstance(evidence, VisualEvidence) and evidence.status == 'PASS'
            and evidence.run_id == state.run_id and evidence.goal_id == state.goal_id
            and evidence.action_id == state.last_action.action_id
            and evidence.state_version == state.state_version and bool(evidence.evidence_refs)
            and evidence.focus_bounds is not None and evidence.confirmed_focus_bounds is not None
            and all(re.fullmatch(r'[0-9a-f]{64}', digest or '') for digest in (
                evidence.before_sha256, evidence.after_sha256, evidence.confirmed_sha256))
            and evidence.before_sha256 != evidence.after_sha256
            and all(isinstance(ref, str) and ref.strip() for ref in evidence.evidence_refs))


def evaluate_goal(contract, state, after, evidence_ref, visual_evidence=None):
    """Only frozen goal predicates and a matching observed action establish PASS."""
    refs = (evidence_ref,) if isinstance(evidence_ref, str) and evidence_ref.strip() else ()
    action_id = state.last_action.action_id if state.last_action is not None else None
    status, reason = 'UNKNOWN', contract.reason
    if contract.kind in {'target_selected', 'open_view', 'tutorial_list', 'tutorial_open'}:
        reason = 'No matching dispatched target action and fresh goal-specific postcondition'
        if _bound_observation(contract, state, after, refs):
            if contract.kind == 'target_selected' and _selected_target(contract, after):
                status, reason = 'PASS', 'The original goal control became selected/checked after its observed click'
            elif _arrived(contract, after):
                status, reason = 'PASS', 'The frozen destination title and required view structure appeared'
            elif contract.kind == 'tutorial_open' and _visual_matches(visual_evidence, state):
                status, reason = 'PASS', 'The original tutorial entry opened a new stable spotlight guide'
                refs = tuple(dict.fromkeys(refs + visual_evidence.evidence_refs))
    return GoalAssessment(state.run_id, state.goal_id, state.state_version,
                          action_id, status, reason, refs)


def apply_goal_assessment(state, assessment):
    """Promote only a completed and step-verified plan with bound goal PASS evidence."""
    verified_steps = {step for action, step in state.action_subgoals
                      if action in state.evaluated_action_ids}
    if (assessment.status != 'PASS' or not assessment.evidence_refs
            or not all(assessment.evidence_refs) or not assessment.reason
            or state.status != GoalStatus.UNKNOWN or state.current_subgoal is not None
            or state.pending_action_id is not None or not state.subgoals
            or any(step not in verified_steps for step in state.subgoals)
            or assessment.run_id != state.run_id or assessment.goal_id != state.goal_id
            or assessment.state_version != state.state_version or state.last_action is None
            or assessment.action_id != state.last_action.action_id
            or assessment.action_id not in state.observed_action_ids
            or assessment.action_id not in state.evaluated_action_ids):
        raise ValueError('Goal PASS requires the completed verified plan and matching goal evidence')
    return replace(state, status=GoalStatus.PASS, revision=state.revision + 1,
                   last_feedback=f'GOAL_VERIFIED:{assessment.evidence_refs[0]}')
