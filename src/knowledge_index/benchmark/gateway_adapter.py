"""The vendored harness's ``ModelAdapter`` interface implemented over our LiteLLM gateway.

Lets us run the vendored ``agent_loop`` unchanged while keeping the transport
ours: OpenAI chat-completions through the gateway, so cost stays tracked and air-gapped
deployments work. Tool definitions are passed through in OpenAI format (the loop is
format-agnostic — it just hands ``tools`` to the adapter).
"""

from __future__ import annotations

from knowledge_index.benchmark import gateway
from knowledge_index.benchmark.agent_harness.base import ModelAdapter, ModelResponse, ToolCall
from knowledge_index.config import AppConfig


class GatewayAdapter(ModelAdapter):
    def __init__(self, config: AppConfig, model: str, *, max_tokens: int = 4000) -> None:
        super().__init__(model, 0.0)
        self.config = config
        self.model = model
        self.max_tokens = max_tokens
        self.usage: dict = {}  # token usage accumulated across the whole agent loop

    @staticmethod
    def _to_openai_tools(tools: list[dict]) -> list[dict]:
        # The harness's canonical tool defs are flat {name, description, parameters};
        # OpenAI chat-completions wants them nested under "function". Accept both.
        out = []
        for tool in tools or []:
            if "function" in tool:
                out.append(tool)
            else:
                out.append({"type": "function", "function": tool})
        return out

    def chat(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        message = gateway.complete(
            self.config,
            self.model,
            messages,
            tools=self._to_openai_tools(tools) or None,
            max_tokens=self.max_tokens,
            usage_sink=self.usage,
        )
        tool_calls = [
            ToolCall(
                id=call["id"],
                name=call["function"]["name"],
                arguments=call["function"].get("arguments") or "{}",
            )
            for call in (message.get("tool_calls") or [])
        ]
        return ModelResponse(
            message=message, tool_calls=tool_calls, text=message.get("content") or ""
        )

    def make_tool_result_messages(self, results: list[tuple[str, str]]) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": call_id, "content": result}
            for call_id, result in results
        ]

    def make_system_message(self, content: str) -> dict:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> dict:
        return {"role": "user", "content": content}
