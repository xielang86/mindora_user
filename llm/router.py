"""llm/router.py — 模型路由：决定每个 LLM 请求走哪个「方向」。

一个「方向」（LLMRoute）= 一组 api_key + api_base + model（+ 可选 endpoint_id）。
api_base / model 放 config.py，api_key 一律从环境变量读取（不落地配置文件）：

  - volc_ark  火山方舟：key=ARK_API_KEY，endpoint=ARK_ENDPOINT_ID
  - kimi      Kimi 编程订阅（Anthropic Messages 协议）：key=KIMI_API_KEY，
              base/model 可用 KIMI_API_BASE / KIMI_MODEL 覆盖

路由规则来自 Config.LLM_ROUTING：request_type → 方向名，"default" 兜底。
两级兜底：
  1. 选路时：所选方向不可用（缺 key）按注册顺序降级到第一个可用方向；
  2. 调用时（available_routes）：首选方向调用失败/超时/返回非 JSON 时，
     调用方按该列表顺序自动切换到其他可用方向（新增 LLM 来源只需在
     from_env 的注册表里加一个 LLMRoute + config.py 配 base/model + 环境变量给 key）。
全不可用返回空列表/None，调用方按「LLM 未启用」处理（用默认文案/兜底策略）。
"""

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional

import requests
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from config import Config
from llm.ark_chat import VolcEngineArkChat


class OpenAICompatibleChat(BaseChatModel):
    """OpenAI 兼容的 chat completions 客户端（Kimi / Moonshot 等，Bearer key 鉴权）。"""

    api_key: str = Field(..., description="API key（Authorization: Bearer）")
    api_base: str = Field(..., description="chat completions 完整 URL")
    model: str = Field(..., description="模型名")
    temperature: float = Field(0.7, description="采样温度")
    max_tokens: Optional[int] = Field(None, description="最大生成 token 数")

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        openai_messages = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                openai_messages.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                openai_messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                openai_messages.append({"role": "assistant", "content": msg.content})

        request_body = {
            "model": self.model,
            "messages": openai_messages,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.max_tokens:
            request_body["max_tokens"] = self.max_tokens
        if stop:
            request_body["stop"] = stop

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        timeout_seconds = int(os.getenv("LLM_TIMEOUT", "120"))
        try:
            response = requests.post(
                url=self.api_base,
                json=request_body,
                headers=headers,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
            answer = result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            resp_info = None
            if "response" in locals():
                try:
                    resp_info = {
                        "status_code": response.status_code,
                        "body": response.content.decode(response.encoding or "utf-8", errors="replace"),
                    }
                except Exception:
                    resp_info = {"status_code": getattr(response, "status_code", None)}
            raise RuntimeError(
                f"调用 OpenAI 兼容 API 失败({self.api_base}): {type(e).__name__}: {e}; response={resp_info}"
            ) from e

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])

    @property
    def _llm_type(self) -> str:
        return "openai-compatible-chat"


class AnthropicCompatibleChat(BaseChatModel):
    """Anthropic Messages 协议客户端（Kimi 编程订阅端点 api.kimi.com/coding 等）。

    与 OpenAI 协议的差异：system 是顶层字段而非一条 message；max_tokens 必填；
    鉴权用 Authorization: Bearer（与 Claude Code 的 ANTHROPIC_AUTH_TOKEN 一致）。
    """

    api_key: str = Field(..., description="API key（Authorization: Bearer）")
    api_base: str = Field(..., description="Anthropic base URL（SDK 自动拼 /v1/messages）")
    model: str = Field(..., description="模型名")
    temperature: float = Field(0.7, description="采样温度")
    max_tokens: int = Field(8192, description="最大生成 token 数（Anthropic 协议必填）")

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        import anthropic

        system_parts: List[str] = []
        anthropic_messages = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                system_parts.append(str(msg.content))
            elif isinstance(msg, HumanMessage):
                anthropic_messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                anthropic_messages.append({"role": "assistant", "content": msg.content})

        timeout_seconds = int(os.getenv("LLM_TIMEOUT", "120"))
        client = anthropic.Anthropic(
            auth_token=self.api_key,  # Bearer，与 Claude Code 订阅端点约定一致
            base_url=self.api_base,
            timeout=timeout_seconds,
        )
        try:
            # 新版 anthropic SDK（kimi coding 端点同构）已移除 messages.create 的
            # temperature 参数（采样档位挪到 output_config.effort），传了会 TypeError，
            # 这里不再传，用服务端默认采样
            resp = client.messages.create(
                model=self.model,
                system="\n\n".join(system_parts) if system_parts else anthropic.NOT_GIVEN,
                messages=anthropic_messages,
                max_tokens=self.max_tokens,
                stop_sequences=stop if stop else anthropic.NOT_GIVEN,
            )
            answer = "".join(b.text for b in resp.content if b.type == "text").strip()
        except Exception as e:
            status = getattr(e, "status_code", None)
            raise RuntimeError(
                f"调用 Anthropic 兼容 API 失败({self.api_base}): {type(e).__name__}: {e}; status_code={status}"
            ) from e

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=answer))])

    @property
    def _llm_type(self) -> str:
        return "anthropic-compatible-chat"


@dataclass(frozen=True)
class LLMRoute:
    """一个请求方向：一组 key + url + model（+ 可选 endpoint_id）。"""

    name: str                                   # 方向名，如 volc_ark / kimi
    kind: str                                   # "volc_ark" | "openai"（OpenAI 兼容协议）| "anthropic"（Anthropic Messages 协议）
    api_key: Optional[str]                      # 来自环境变量
    api_base: str                               # 来自 config.py
    model: str                                  # 来自 config.py（可被环境变量覆盖）
    endpoint_id: Optional[str] = None           # 仅 volc_ark 用
    temperature: float = 0.5

    @property
    def available(self) -> bool:
        return bool(self.api_key)


class ModelRouter:
    """按 Config.LLM_ROUTING 规则为每个 request_type 选择请求方向，并缓存各方向的 chat model。"""

    def __init__(self, routes: dict[str, LLMRoute], rules: dict[str, str]):
        self._routes = routes
        self._rules = rules
        self._models: dict[tuple[str, Optional[float]], BaseChatModel] = {}

    @classmethod
    def from_env(cls) -> "ModelRouter":
        """从 config.py（url/model）+ 环境变量（key）构建方向注册表与路由规则。"""
        routes = {
            "volc_ark": LLMRoute(
                name="volc_ark",
                kind="volc_ark",
                api_key=os.getenv("ARK_API_KEY"),
                api_base=Config.ARK_API_BASE,
                model=os.getenv("ARK_MODEL") or Config.ARK_MODEL,
                endpoint_id=os.getenv("ARK_ENDPOINT_ID", "ep-20260325170723-znh7n"),
            ),
            "kimi": LLMRoute(
                name="kimi",
                kind="anthropic",  # Kimi 编程订阅端点是 Anthropic Messages 协议
                api_key=os.getenv("KIMI_API_KEY"),
                api_base=os.getenv("KIMI_API_BASE") or Config.KIMI_API_BASE,
                model=os.getenv("KIMI_MODEL") or Config.KIMI_MODEL,
            ),
        }
        return cls(routes, dict(Config.LLM_ROUTING))

    def route_for(self, request_type: str) -> Optional[LLMRoute]:
        """决定 request_type 的请求方向；所选方向缺 key 时降级到第一个可用方向。"""
        wanted = self._rules.get(request_type) or self._rules.get("default")
        route = self._routes.get(wanted) if wanted else None
        if route is not None and route.available:
            return route
        if wanted:
            logging.warning(
                "llm route %s unavailable (missing api key or unknown), request_type=%s, falling back",
                wanted, request_type,
            )
        for fallback in self._routes.values():
            if fallback.available:
                logging.info(
                    "llm route fallback: %s -> %s (request_type=%s)",
                    wanted, fallback.name, request_type,
                )
                return fallback
        return None

    def available_routes(self, request_type: str) -> list["LLMRoute"]:
        """按优先级返回 request_type 的所有可用方向（首选在前，其余按注册顺序）。

        调用方据此做调用级故障转移：首选方向调用失败/超时/非 JSON 时按列表顺序
        切换到下一个方向。空列表 = 全不可用（按 LLM 未启用处理）。
        """
        wanted = self._rules.get(request_type) or self._rules.get("default")
        ordered: list[LLMRoute] = []
        if wanted and wanted in self._routes:
            ordered.append(self._routes[wanted])
        ordered.extend(r for name, r in self._routes.items() if name != wanted)
        return [r for r in ordered if r.available]

    def chat_model_for_route(
        self,
        route: "LLMRoute",
        temperature: Optional[float] = None,
    ) -> Optional[BaseChatModel]:
        """返回指定方向的 chat model（按 方向+温度 缓存）；方向不可用返回 None。"""
        if not route.available:
            return None
        key = (route.name, temperature)
        if key not in self._models:
            self._models[key] = self._build_chat_model(route, temperature)
            logging.info(
                "llm chat model created: route=%s model=%s api_base=%s",
                route.name, route.model, route.api_base,
            )
        return self._models[key]

    def chat_model_for(
        self,
        request_type: str,
        temperature: Optional[float] = None,
    ) -> Optional[BaseChatModel]:
        """返回 request_type 对应方向的 chat model（按 方向+温度 缓存）；无可用方向返回 None。"""
        route = self.route_for(request_type)
        if route is None:
            return None
        key = (route.name, temperature)
        if key not in self._models:
          self._models[key] = self._build_chat_model(route, temperature)
          logging.info(
              "llm chat model created: route=%s model=%s api_base=%s",
              route.name, route.model, route.api_base,
          )
        return self._models[key]

    @staticmethod
    def _build_chat_model(route: LLMRoute, temperature: Optional[float]) -> BaseChatModel:
        temp = route.temperature if temperature is None else temperature
        if route.kind == "volc_ark":
            return VolcEngineArkChat(
                ark_api_key=route.api_key,
                endpoint_id=route.endpoint_id or "",
                model=route.model,
                api_base=route.api_base,
                temperature=temp,
            )
        if route.kind == "anthropic":
            return AnthropicCompatibleChat(
                api_key=route.api_key,
                api_base=route.api_base,
                model=route.model,
                temperature=temp,
            )
        return OpenAICompatibleChat(
            api_key=route.api_key,
            api_base=route.api_base,
            model=route.model,
            temperature=temp,
        )
