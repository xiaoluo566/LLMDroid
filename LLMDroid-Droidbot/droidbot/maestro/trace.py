"""Append structured role and device events to a fresh per-run JSONL file."""

import json
import re
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import Lock


_REDACTED = frozenset({
    'apikey', 'authorization', 'bearer', 'cookie', 'credentials', 'password',
    'secret', 'token', 'accesstoken', 'refreshtoken', 'rawpayload', 'payload',
})
_ENVELOPE_KEYS = frozenset({'seq', 'timestamp', 'role', 'event'})


def _serializable(value):
    if isinstance(value, Enum):
        return _serializable(value.value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _serializable({field.name: getattr(value, field.name) for field in fields(value)})
    if isinstance(value, Mapping):
        output = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError('Trace mapping keys must be strings')
            normal = re.sub(r'[^a-z0-9]', '', key.lower())
            output[key] = '[redacted]' if normal in _REDACTED else _serializable(item)
        return output
    if isinstance(value, (tuple, list)):
        return [_serializable(item) for item in value]
    raise TypeError(f'Unsupported trace detail type: {type(value).__name__}')


class JsonlTrace:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=False)
        self._sequence = 0
        self._lock = Lock()

    def write(self, role, event, **details):
        if not all(isinstance(item, str) and item.strip() for item in (role, event)):
            raise ValueError('Trace role and event must be non-empty strings')
        if _ENVELOPE_KEYS.intersection(details):
            raise ValueError('Trace details cannot overwrite the event envelope')
        safe_details = _serializable(details)
        with self._lock:
            row = dict(seq=self._sequence + 1, timestamp=datetime.now(timezone.utc).isoformat(),
                       role=role, event=event, **safe_details)
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False)
            with self.path.open('a', encoding='utf-8', newline='\n') as output:
                output.write(encoded + '\n')
            self._sequence += 1
        return row
