"""LLM에게 보이는 ID를 짧은 이름표로 바꾼다 (메일 M1, 업체 S1, 등록 품목 I1, 견적 Q1, 초안 D1).

LLM이 36자 UUID를 도구 인자·근거·최종 답에 옮겨 적다가 한 글자만 틀려도 실행이 실패했다
(2026-11-09 브로콜리 통보: 근거 메일 ID 6곳에 "4d5d-"가 한 번 더 들어감). 그래서 실행 안에서는 이름표만 보인다.
- 에이전트 입력·도구 결과의 ID → 이름표 (hide)
- LLM이 쓴 도구 인자·최종 답의 이름표 → ID (reveal). 도구·저장소·DB·그래프 상태는 지금처럼 ID만 다룬다
어떤 키가 무슨 ID인지는 키 이름으로 정한다(ID_KEYS). 이름표가 아닌 값(ID 그대로, 모르는 값)은 바꾸지 않고 넘겨
도구가 원래대로 검사·거절한다. 이름표는 역할 호출 한 번(AgentRunner 한 번) 안에서만 쓴다.
"""
import json
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

# 키 이름 → 이름표 앞글자. 값이 문자열 하나든 문자열 목록이든 같은 종류다
ID_KEYS = {
    'mail_id': 'M', 'mail_ids': 'M', 'source_mail_id': 'M',
    'supplier_id': 'S', 'supplier_ids': 'S', 'target_supplier_id': 'S', 'rival_supplier_id': 'S',
    'supplier_item_id': 'I', 'supplier_item_ids': 'I',
    'quote_version_id': 'Q', 'quote_version_ids': 'Q', 'latest_quote_version_ids': 'Q', 'quote_id': 'Q',
    'supersedes_id': 'Q', 'target_quote_version_id': 'Q', 'rival_quote_version_id': 'Q',
    'basis_quote_version_ids': 'Q', 'source_quote_ids': 'Q',
    'draft_id': 'D', 'draft_ids': 'D',
}


class IdHandles:
    """역할 호출 한 번의 ID ↔ 이름표 대응표. 처음 본 순서대로 종류마다 1, 2, 3…을 붙인다."""

    def __init__(self):
        self.by_id: dict[str, str] = {}
        self.by_handle: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def handle(self, prefix: str, value: str) -> str:
        """ID의 이름표. 처음 보는 ID면 새 이름표를 만든다."""
        if value not in self.by_id:
            self.counts[prefix] = self.counts.get(prefix, 0) + 1
            name = f'{prefix}{self.counts[prefix]}'
            self.by_id[value], self.by_handle[name] = name, value
        return self.by_id[value]

    def resolve(self, value: str) -> str:
        """이름표면 원래 ID, 아니면 그대로 (ID를 그대로 쓴 경우·모르는 값은 도구가 판단한다)."""
        return self.by_handle.get(value.strip(), value) if isinstance(value, str) else value

    def hide(self, data: Any, prefix: str | None = None) -> Any:
        """dict·list 안의 ID 키 값을 이름표로 바꾼 사본. 이미 이름표인 값은 그대로 둔다."""
        if isinstance(data, dict):
            return {key: self.hide(value, ID_KEYS.get(key)) for key, value in data.items()}
        if isinstance(data, list):
            return [self.hide(value, prefix) for value in data]
        if prefix and isinstance(data, str) and data and data not in self.by_handle:
            return self.handle(prefix, data)
        return data

    def reveal(self, data: Any, inside_id: bool = False) -> Any:
        """dict·list 안의 ID 키 값이 이름표면 원래 ID로 바꾼 사본."""
        if isinstance(data, dict):
            return {key: self.reveal(value, key in ID_KEYS) for key, value in data.items()}
        if isinstance(data, list):
            return [self.reveal(value, inside_id) for value in data]
        if inside_id and isinstance(data, str):
            return self.resolve(data)
        return data

    def hide_json(self, text: str) -> str:
        """JSON 문자열(도구 결과·에이전트 입력)의 ID를 이름표로. JSON이 아니면 그대로."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return text
        return json.dumps(self.hide(data), ensure_ascii=False)

    def reveal_json(self, text: str) -> str:
        """JSON 문자열의 이름표를 ID로. JSON이 아니면 그대로."""
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            return text
        return json.dumps(self.reveal(data), ensure_ascii=False)

    def reveal_messages(self, messages: list) -> list:
        """도구 결과 메시지의 이름표를 ID로 바꾼 사본 목록. 최종 검사가 도구 결과와 ID로 대조할 수 있게 한다."""
        return [message.model_copy(update={'content': self.reveal_json(message.content)})
                if isinstance(message, ToolMessage) and isinstance(message.content, str) else message
                for message in messages]


class HandleMiddleware(AgentMiddleware):
    """도구 호출마다 LLM 인자의 이름표를 ID로 바꿔 도구에 넘기고, 도구 결과의 ID를 이름표로 바꿔 LLM에 돌려준다.
    실행 정보(RunContext)에 대응표가 없으면(테스트에서 에이전트를 직접 부를 때) 그대로 넘긴다."""

    def wrap_tool_call(self, request, handler):
        handles = getattr(getattr(request.runtime, 'context', None), 'handles', None)
        if handles is None:
            return handler(request)
        call = dict(request.tool_call) | {'args': handles.reveal(request.tool_call.get('args') or {})}
        result = handler(request.override(tool_call=call))
        if isinstance(result, ToolMessage) and isinstance(result.content, str):
            return result.model_copy(update={'content': handles.hide_json(result.content)})
        return result
