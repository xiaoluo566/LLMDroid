"""Adapt observed DroidBot pages and actions without sending device events."""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import islice

from .goal_selection import GoalCandidate
from .guidance_pool import guidance_overview
from .schemas import ActionKind, GoalStatus, PageSnapshot, WidgetSnapshot


_SWIPE_DIRECTIONS = {
    ActionKind.SWIPE_UP: "up", ActionKind.SWIPE_DOWN: "down",
    ActionKind.SWIPE_LEFT: "left", ActionKind.SWIPE_RIGHT: "right",
}
_HORIZONTAL_SWIPES = {ActionKind.SWIPE_LEFT, ActionKind.SWIPE_RIGHT}
_NATIVE_ACTIONS = {
    "CLICK": ActionKind.CLICK, "LONG_CLICK": ActionKind.LONG_PRESS, "INPUT": ActionKind.TEXT,
    "SCROLL_BOTTOM_UP": ActionKind.SWIPE_UP, "SCROLL_TOP_DOWN": ActionKind.SWIPE_DOWN,
    "SCROLL_RIGHT_LEFT": ActionKind.SWIPE_LEFT, "SCROLL_LEFT_RIGHT": ActionKind.SWIPE_RIGHT,
}


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


def _observed_bounds(view, dimensions):
    try:
        (left, top), (right, bottom) = view["bounds"]
    except (KeyError, TypeError, ValueError):
        return None
    if not (all(type(coordinate) is int for coordinate in (left, top, right, bottom))
            and 0 <= left < right and 0 <= top < bottom):
        return None
    center = ((left + right) // 2, (top + bottom) // 2)
    if any(dimension is not None and coordinate >= dimension
           for coordinate, dimension in zip(center, dimensions)):
        return None
    return left, top, right, bottom


def _widget_kind(view):
    if view.get("editable"):
        return "input"
    if view.get("clickable") or view.get("checkable"):
        return "button"
    if view.get("scrollable"):
        return "scroller"
    return "label"


def _native_events_by_view(state):
    collect = getattr(state, "get_possible_input", None)
    events = collect() if callable(collect) else None
    if not isinstance(events, (list, tuple)):
        return None
    by_view = defaultdict(dict)
    for event in events:
        kind = _NATIVE_ACTIONS.get(event.get_action_type().name)
        if kind is not None and event.view is not None:
            by_view[id(event.view)].setdefault(kind, event)
    return by_view


def _declared_actions(view):
    # Adapter fixtures without a native event inventory expose accessibility flags.
    actions = set()
    if view.get("clickable") or view.get("checkable"):
        actions.add(ActionKind.CLICK)
    if view.get("long_clickable"):
        actions.add(ActionKind.LONG_PRESS)
    if view.get("editable"):
        actions.add(ActionKind.TEXT)
    if view.get("scrollable"):
        actions.update(_SWIPE_DIRECTIONS)
    return actions


def _swipe_fits(kind, event, bounds, dimensions):
    axis = 0 if kind in _HORIZONTAL_SWIPES else 1
    limit = dimensions[axis]
    if limit is None or bounds[axis + 2] <= limit:
        return True
    if event is None:
        return False
    return all(0 <= point[axis] < limit for point in event.get_drag_points())


def _allowed_actions(view, native_events, bounds, dimensions):
    events = {} if native_events is None else native_events.get(id(view), {})
    actions = _declared_actions(view) if native_events is None else set(events)
    if ActionKind.TEXT in actions:
        actions.add(ActionKind.ENTER)
    actions = {kind for kind in actions if kind not in _SWIPE_DIRECTIONS
               or _swipe_fits(kind, events.get(kind), bounds, dimensions)}
    return tuple(sorted(actions, key=lambda item: item.value))


def _scroll_axis(view):
    get_scroll_type = getattr(view.get("widget"), "get_scroll_type", None)
    if not view.get("scrollable") or not callable(get_scroll_type):
        return None
    return {"Horizontal": "horizontal", "Vertical": "vertical", "ALL": "both"}.get(
        get_scroll_type().name)


def _visible_rows(state):
    package = _package_name(state)
    dimensions = _viewport_dimensions(state)
    native_events = _native_events_by_view(state)
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
        bounds = _observed_bounds(view, dimensions)
        safe = view.get("enabled") and bounds is not None
        kind = _widget_kind(view) if safe else "label"
        actions = _allowed_actions(view, native_events, bounds, dimensions) if safe else ()
        if kind == "label" and not actions and not label and not resource:
            continue
        # Input identities survive text edits. Other controls include their label
        # to distinguish separate rows that reuse the same resource ID.
        identity_label = label
        if view.get("editable"):
            identity_label = "" if resource else _text(view, "content_description")
        identity = [state.foreground_activity, resource, _text(view, "class"), identity_label]
        encoded = json.dumps(identity, ensure_ascii=False).encode("utf-8")
        identifier = "w" + hashlib.sha256(encoded).hexdigest()[:16]
        yield identifier, kind, resource, label, actions, bounds, view


def _widget_bindings(state):
    rows = tuple(_visible_rows(state))
    counts = Counter(row[0] for row in rows)
    occurrences = Counter()
    for identifier, kind, resource, label, actions, bounds, view in rows:
        if counts[identifier] > 1:
            # Identical repeated controls have no safe target identity across captures.
            occurrences[identifier] += 1
            identifier = f"{identifier}-{occurrences[identifier]}"
            kind = "label"
            actions = ()
        yield WidgetSnapshot(identifier, kind, label, resource or None,
                             selected=view.get('selected') is True,
                             checked=view.get('checked') is True,
                             checkable=view.get('checkable') is True,
                             supported_actions=actions, bounds=bounds,
                             scroll_axis=_scroll_axis(view)), view


def snapshot_from_device(run_id, version, device_state):
    """Keep the raw page fingerprint even when the observation has no UTG ID."""
    widgets = tuple(widget for widget, _ in _widget_bindings(device_state))
    return PageSnapshot(run_id, version, device_state.state_str, widgets)


@dataclass(frozen=True)
class GuidanceFunction:
    candidate_id: str
    cluster_id: str
    function: str
    reason: str = ""


@dataclass(frozen=True)
class CandidatePool:
    functions: tuple[GuidanceFunction, ...]
    candidates: tuple[GoalCandidate, ...]

    @property
    def rejected(self):
        return tuple(item for item in self.functions if item.reason)


def _bind_candidate(run_id, cluster, item, package_name, observations):
    state = cluster.get_target_state(item.function)
    if state is None or state.get_id() < 0:
        return None, "missing_target_state"
    if _package_name(state) != package_name:
        return None, "foreign_target_state"
    original = cluster.get_function_target_widget(item.function)
    if original is None:
        return None, "unbound_target"
    key = id(state)
    if key not in observations:
        try:
            observations[key] = tuple(_widget_bindings(state))
        except OSError:
            observations[key] = None
    bindings = observations[key]
    if bindings is None:
        return None, "observation_unavailable"
    matches = [widget for widget, view in bindings
               if view.get("widget") is original and widget.allowed_actions]
    if len(matches) != 1:
        return None, "unbound_target"
    state_id = state.get_id()
    return GoalCandidate(
        run_id, item.candidate_id, str(state_id), item.function,
        f"states/state_{state_id}.json", item.cluster_id,
        tuple(widget for widget, _ in bindings), matches[0].widget_id,
    ), ""


def read_candidate_pool(run_id, utg, package_name, clusters=None):
    """Keep baseline entries and report which ones can bind to a safe native action."""
    ranked = tuple(islice(utg.clusters if clusters is None else clusters, 10))
    overview = guidance_overview(ranked)
    by_key = {f"State{cluster.get_id()}": cluster for cluster in ranked}
    functions, candidates, observations = [], [], {}
    # Match the original Guidance dictionary, where repeated ranked clusters
    # contribute one entry and keep the position of their first occurrence.
    for key, entry in overview.items():
        cluster = by_key[key]
        cluster_id = str(cluster.get_id())
        names = entry.get("FunctionList", ())
        for number, name in enumerate(names, start=1):
            item = GuidanceFunction(f"c{cluster_id}-f{number}", cluster_id, name)
            candidate, reason = _bind_candidate(run_id, cluster, item, package_name, observations)
            functions.append(GuidanceFunction(item.candidate_id, cluster_id, name, reason))
            if candidate is not None:
                candidates.append(candidate)
    return CandidatePool(tuple(functions), tuple(candidates))


def candidates_from_utg(run_id, utg, package_name, clusters=None):
    return read_candidate_pool(run_id, utg, package_name, clusters).candidates


def _event_classes():
    from ..input_event import SetTextEvent, TouchEvent
    return TouchEvent, SetTextEvent


def _key_event_class():
    from ..input_event import KeyEvent
    return KeyEvent


def _gesture_event(kind, view):
    from ..input_event import LongTouchEvent, ScrollEvent
    if kind == ActionKind.LONG_PRESS:
        return LongTouchEvent(view=view)
    return ScrollEvent(view=view, direction=_SWIPE_DIRECTIONS[kind])


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
    if not observed_widget.supports(action.kind):
        raise ValueError("当前控件不支持待执行动作")

    if action.kind == ActionKind.CLICK:
        touch_event, _ = _event_classes()
        return touch_event(view=view)
    if action.kind == ActionKind.ENTER:
        if not view.get("focused"):
            raise ValueError("Enter 目标输入框尚未获得焦点")
        return _key_event_class()(name="ENTER")
    if action.kind == ActionKind.TEXT:
        value = action.value if action.value_ref is None else goal_state.get_variable(action.value_ref)
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError("输入数据没有确认或包含控制字符")
        _, set_text_event = _event_classes()
        return set_text_event(view=view, text=value)
    return _gesture_event(action.kind, view)
