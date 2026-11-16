"""Validate an already planned Goal against the page actually reached."""

from .goal_selection import step_binding
from .schemas import ActionKind


def candidate_origin(candidate):
    """An ordinal candidate ID can change when the ranked FunctionList changes."""
    return (candidate.target_cluster_id, candidate.state_id,
            candidate.function, candidate.target_widget_id)


def _identity(widget):
    return widget.resource_id, widget.text, widget.widget_type


def anchor_problem(candidate, page, operations):
    target_id = candidate.target_widget_id
    original = next((widget for widget in candidate.widgets if widget.widget_id == target_id), None)
    target = next((widget for widget in page.widgets if widget.widget_id == target_id), None)
    if target is None or original is None:
        return 'goal_anchor_missing'

    required = {kind for kind, widget_id in operations if widget_id == target_id}
    if not target.allowed_actions or not required.issubset(target.allowed_actions):
        return 'goal_anchor_action_unavailable'
    if _identity(target) != _identity(original):
        return 'goal_anchor_changed'
    return None


def arrival_problem(candidate, page, steps):
    operations = tuple(step_binding(step) for step in steps)
    problem = anchor_problem(candidate, page, operations)
    if problem:
        return problem
    kind, first_id = operations[0]
    first = next((widget for widget in page.widgets if widget.widget_id == first_id), None)
    if first is None:
        return 'first_step_target_missing'
    if not first.supports(kind):
        return 'first_step_action_unavailable'
    if kind == ActionKind.ENTER and not first.focused:
        return 'first_step_enter_not_focused'
    return None
