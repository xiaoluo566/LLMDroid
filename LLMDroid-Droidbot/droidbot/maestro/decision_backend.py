from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol


class ResponseFormatError(ValueError):
    """模型响应不是可用的 JSON 对象。"""


@dataclass(frozen=True)
class ModelRequest:
    role: str
    request_id: str
    payload: Dict[str, Any]
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class ModelResponse:
    request_id: str
    data: Dict[str, Any]
    input_tokens: int = 0
    output_tokens: int = 0


class DecisionBackend(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        ...


def parse_json_object(raw_text: str) -> Dict[str, Any]:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ResponseFormatError("模型响应不是合法 JSON") from error
    if not isinstance(data, dict):
        raise ResponseFormatError("模型响应必须是 JSON 对象")
    return data


class CallableDecisionBackend:
    """把原 LLMAgent 或其他模型调用包装成统一协议。"""

    def __init__(self, caller: Callable[[ModelRequest], ModelResponse]):
        self._caller = caller

    def complete(self, request: ModelRequest) -> ModelResponse:
        response = self._caller(request)
        if not isinstance(response, ModelResponse):
            raise TypeError("模型适配器必须返回 ModelResponse")
        return response


class FakeDecisionBackend:
    """单元测试用后端，不访问网络或设备。"""

    def __init__(self, responses: Optional[Dict[str, Dict[str, Any]]] = None):
        self.responses = dict(responses or {})
        self.requests = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            request_id=request.request_id,
            data=dict(self.responses.get(request.role, {})),
        )
