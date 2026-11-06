from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol


class ResponseFormatError(ValueError):
    """模型响应不是可用的 JSON 对象。"""

    def __init__(self, message, input_tokens=None, output_tokens=None):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


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
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class DecisionBackend(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        ...


class OpenAICompatibleBackend:
    """复用原论文使用的 Chat Completions 接口；调用方注入配置好的客户端。"""

    def __init__(self, client: Any, model: str):
        if not model:
            raise ValueError("模型名称不能为空")
        self._client = (
            client.with_options(max_retries=0)
            if callable(getattr(client, "with_options", None))
            else client
        )
        self._model = model

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.request_id or not request.role or request.timeout_seconds <= 0:
            raise ValueError("模型请求缺少标识或超时时间无效")
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": json.dumps(
                request.payload, ensure_ascii=False)}],
            temperature=0,
            timeout=request.timeout_seconds,
        )
        usage = getattr(response, "usage", None)
        try:
            data = parse_json_object(response.choices[0].message.content)
        except ResponseFormatError as error:
            raise ResponseFormatError(
                "模型响应格式无效",
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
            ) from error
        return ModelResponse(
            request_id=request.request_id,
            data=data,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
        )


def parse_json_object(raw_text: str) -> Dict[str, Any]:
    if not isinstance(raw_text, str):
        raise ResponseFormatError("模型响应必须是文本")
    fenced = re.fullmatch(r"\s*\x60{3}(?:json)?\s*\n(.*?)\n\x60{3}\s*",
                          raw_text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        raw_text = fenced.group(1)
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
