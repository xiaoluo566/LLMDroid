"""Adapt observed DroidBot pages and actions without sending device events."""

import hashlib
import json
from collections import Counter
from itertools import islice

from .goal_selection import GoalCandidate
from .schemas import ActionKind, GoalStatus, PageSnapshot, WidgetSnapshot


def _package_name(state):
    return (state.foreground_activity or "").partition("/")[0]


def _text(view, key):
    value = view.get(key)
    return value if isinstance(value, str) else ""


def _viewport_dimensions(state):
    screenshot = getattr(state, 'screenshot_path', None)
    if isinstance(screenshot, str) and screenshot:
        from PIL import Image
        with Image.open(screenshot) as image:
            return image.size
    return getattr(state, 'width', None), getattr(state, 'height', None)


def _bounds_are_valid(view, dimensions):
    try:
        (left, top), (right, bottom) = view["bounds"]
    except (KeyError, TypeError, ValueError):
        return False
    if not (all(isinstance(coordinate, int) for coordinate in (left, top, right, bottom))
            and 0 <= left < right and 0 <= top < bottom):
        return False
    center = ((left + right) // 2, (top + bottom) // 2)
    return all(dimension is None or coordinate < dimension
               for coordinate, dimension in zip(center, dimensions))


def _widget_kind(view, dimensions):
    if not view.get("enabled") or not _bounds_are_valid(view, dimensions):
        return "label"
    if view.get("editable"):
        return "input"
    if view.get("clickable") or view.get("checkable"):
        return "button"
    return "label"


def _visible_rows(state):
    package = _package_name(state)
    dimensions = _viewport_dimensions(state)
    for view in state.views:
        if not view.get("visible") or view.get("password") or view.get("is_password"):
            continue
        if view.get("package") and view["package"] != package:
            continue
        resource = _text(view, "resource_id")
        if resource in {"android:id/navigationBarBackground", "android:id/statusBarBackground"}:
            continue
        if ":id/" in resource and resource.partition(":id/")[0] not in {package, "android"}:
            continue
        label = _text(view, "text")
        if not view.get("editable"):
            label = label or _text(view, "content_description")
        kind = _widget_kind(view, dimensions)
        if kind == "label" and not label and not resource:
            continue
        # Input identities survive text edits. Other controls include their label
        # to distinguish separate rows that reuse the same resource ID.
        identity_label = label
        if view.get("editable"):
            identity_label = "" if resource else _text(view, "content_description")
        identity = [state.foreground_activity, resource, _text(view, "class"), identity_label]
        encoded = json.dumps(identity, ensure_ascii=False).encode("utf-8")
        yield "w" + hashlib.sha256(encoded).hexdigest()[:16], kind, resource, label, view


def _widget_bindings(state):
    rows = tuple(_visible_rows(state))
    counts = Counter(row[0] for row in rows)
    occurrences = Counter()
    for identifier, kind, resource, label, view in rows:
        if counts[identifier] > 1:
            # Identical repeated controls have no safe target identity across captures.
            occurrences[identifier] += 1
            identifier = f"{identifier}-{occurrences[identifier]}"
            kind = "label"
        yield WidgetSnapshot(identifier, kind, label, resource or None), view


def snapshot_from_device(run_id, version, device_state):
    """Keep the raw page fingerprint even when the observation has no UTG ID."""
    widgets = tuple(widget for widget, _ in _widget_bindings(device_state))
    return PageSnapshot(run_id, version, device_state.state_str, widgets)


def _candidate_targets(clusters, package_name, include_tested):
    for cluster in clusters:
        if not include_tested and not cluster.has_untested_function():
            continue
        overview = {}
        cluster.write_overview_top5_tojson(overview, ignore_importance=include_tested)
        cluster_id = cluster.get_id()
        functions = overview.get(f"State{cluster_id}", {}).get("FunctionList", ())
        for number, function in enumerate(functions, start=1):
            state = cluster.get_target_state(function)
            if state is not None and _package_name(state) == package_name and state.get_id() >= 0:
                yield cluster_id, number, function, state


def candidates_from_utg(run_id, utg, package_name, clusters=None):
    """Read LLMDroid's ranked Function List and resolve its original UTG states."""
    eligible_clusters = tuple(islice(utg.clusters if clusters is None else clusters, 10))
    for include_tested in (False, True):
        targets = _candidate_targets(eligible_clusters, package_name, include_tested)
        candidates = []
        for cluster_id, number, function, state in targets:
            widgets = snapshot_from_device(run_id, 0, state).widgets
            if not any(w.widget_type in {"input", "button"} for w in widgets):
                continue
            state_id = state.get_id()
            candidates.append(GoalCandidate(
                run_id, f"c{cluster_id}-f{number}", str(state_id), function,
                f"states/state_{state_id}.json", str(cluster_id), widgets,
            ))
        if candidates:
            return tuple(candidates)
    return ()


def _event_classes():
    from ..input_event import SetTextEvent, TouchEvent
    return TouchEvent, SetTextEvent


def _key_event_class():
    from ..input_event import KeyEvent
    return KeyEvent


def bind_event(action, state, snapshot, goal_state):
    """Convert the pending action using a fresh observation of the same page."""
    if (goal_state.status != GoalStatus.ACTIVE
            or snapshot.run_id != goal_state.run_id
            or snapshot.state_version != goal_state.state_version
            or action.source_state_version != snapshot.state_version
            or goal_state.pending_action_id != action.action_id
            or goal_state.last_action != action
            or state.state_str != snapshot.state_id):
        raise ValueError("设备动作与当前任务或页面观察不匹配")

    fresh = {widget.widget_id: (widget, view) for widget, view in _widget_bindings(state)}
    try:
        planned_widget = snapshot.find_widget(action.widget_id)
        observed_widget, view = fresh[action.widget_id]
    except KeyError as exc:
        raise ValueError("待操作控件已不在当前页面") from exc
    if planned_widget != observed_widget:
        raise ValueError("待操作控件在发送前发生变化")

    if action.kind == ActionKind.CLICK and observed_widget.widget_type == "button":
        touch_event, _ = _event_classes()
        return touch_event(view=view)
    if action.kind == ActionKind.ENTER and observed_widget.widget_type == "input":
        if not view.get("focused"):
            raise ValueError("Enter 目标输入框尚未获得焦点")
        return _key_event_class()(name="ENTER")
    if action.kind == ActionKind.TEXT and observed_widget.widget_type == "input":
        value = action.value if action.value_ref is None else goal_state.get_variable(action.value_ref)
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError("输入数据没有确认或包含控制字符")
        _, set_text_event = _event_classes()
        return set_text_event(view=view, text=value)
    raise ValueError("当前控件不支持待执行动作")
