"""Bound, local evidence for a stable circular spotlight; not general visual semantics."""

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image


@dataclass(frozen=True)
class VisualEvidence:
    run_id: str
    goal_id: str
    action_id: str
    state_version: int
    status: str
    reason: str
    before_sha256: str
    after_sha256: str
    confirmed_sha256: str
    focus_bounds: Optional[Tuple[int, int, int, int]]
    confirmed_focus_bounds: Optional[Tuple[int, int, int, int]]
    evidence_refs: Tuple[str, ...]


def _load(path):
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with Image.open(path) as source:
        if source.width > 10000 or source.height > 10000:
            raise ValueError('Screenshot dimensions exceed the observation limit')
        size = source.size
        image = source.convert('RGB')
    image.thumbnail((240, 600))
    return size, image, digest


def _components(image):
    width, height = image.size
    gray = image.convert('L')
    pixels = gray.load()
    top = int(height * .04)
    seen = set()
    for y in range(top, height):
        for x in range(width):
            if (x, y) in seen or pixels[x, y] < 205:
                continue
            pending, component = [(x, y)], []
            seen.add((x, y))
            while pending:
                point = pending.pop()
                component.append(point)
                for nx, ny in ((point[0]-1, point[1]), (point[0]+1, point[1]),
                               (point[0], point[1]-1), (point[0], point[1]+1)):
                    if (0 <= nx < width and top <= ny < height
                            and (nx, ny) not in seen and pixels[nx, ny] >= 205):
                        seen.add((nx, ny))
                        pending.append((nx, ny))
            yield component


def _colored_halo(image, box):
    left, top, right, bottom = box
    cx, cy = (left+right)/2, (top+bottom)/2
    diameter = max(right-left, bottom-top)
    # A spotlight may have a thin or wide halo. Check fixed concentric bands,
    # retaining the same angular coverage requirement for each complete band.
    for factor in (.525, .55, .575, .6, .65, .7, .75):
        radius, samples = diameter*factor, []
        for index in range(48):
            angle = index * math.pi / 24
            x, y = round(cx + radius*math.cos(angle)), round(cy + radius*math.sin(angle))
            if 0 <= x < image.width and 0 <= y < image.height:
                rgb = image.getpixel((x, y))
                samples.append(max(rgb)-min(rgb) >= 12)
        if samples and sum(samples)/len(samples) >= .55:
            return True
    return False


def _spotlight_cores(image):
    gray = image.convert('L')
    pixels = tuple(gray.getdata())
    if sum(value < 110 for value in pixels)/len(pixels) < .55:
        return
    for component in _components(image):
        if len(component) < image.width*image.height*.002:
            continue
        xs, ys = zip(*component)
        box = min(xs), min(ys), max(xs)+1, max(ys)+1
        width, height = box[2]-box[0], box[3]-box[1]
        density = len(component)/(width*height)
        if (.65 <= width/height <= 1.6 and .62 <= density <= .9
                and width >= image.width*.1 and width <= image.width*.65):
            yield box


def _unique_core(image):
    cores = tuple(_spotlight_cores(image))
    return cores[0] if len(cores) == 1 else None


def _stable(first, second, size):
    if first is None or second is None:
        return False
    tolerance = max(3, size[0]*.025)
    return all(abs(a-b) <= tolerance for a, b in zip(first, second))


def inspect_tutorial_overlay(run_id, goal_id, action_id, state_version,
                             before_path, after_path, confirmed_path, evidence_refs):
    """Require a stable new focus and dark mask with a halo visible in at least one frame."""
    hashes, focus, confirmed_focus = ('', '', ''), None, None
    status, reason = 'UNKNOWN', 'No new stable spotlight evidence'
    refs = tuple(evidence_refs) if isinstance(evidence_refs, (list, tuple)) else ()
    try:
        if (not all(isinstance(item, str) and item.strip() for item in (run_id, goal_id, action_id))
                or type(state_version) is not int or state_version < 0
                or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs)):
            raise ValueError('Visual observation identifiers or evidence references are invalid')
        before_size, before, before_hash = _load(before_path)
        after_size, after, after_hash = _load(after_path)
        confirmed_size, confirmed, confirmed_hash = _load(confirmed_path)
        hashes = before_hash, after_hash, confirmed_hash
        if (before_size != after_size or after_size != confirmed_size
                or Path(after_path).resolve() == Path(confirmed_path).resolve()):
            raise ValueError('Visual evidence needs matching dimensions and an independent recapture')
        focus, confirmed_focus = _unique_core(after), _unique_core(confirmed)
        if (before_hash != after_hash and _stable(focus, confirmed_focus, after.size)
                and next(_spotlight_cores(before), None) is None
                and (_colored_halo(after, focus) or _colored_halo(confirmed, confirmed_focus))):
            status = 'PASS'
            reason = 'A new circular spotlight is stable across two observations with a colored halo observed'
    except (OSError, TypeError, ValueError):
        reason = 'Visual screenshots or their observation binding are unavailable'
    return VisualEvidence(run_id, goal_id, action_id, state_version, status, reason,
                          *hashes, focus, confirmed_focus, refs)
