import os
import requests
from typing import List, Optional, Any
from pydantic import Field, model_validator

# 导入LangChain核心抽象类和消息类型（遵循标准）
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.callbacks import CallbackManagerForLLMRun

# 自定义火山方舟平台Chat类：继承LangChain标准BaseChatModel，适配ark_api_key
class VolcEngineArkChat(BaseChatModel):
    """火山引擎方舟大模型LangChain集成类（适配ark_api_key单密钥鉴权）"""
    # 方舟平台必传/可选参数（Pydantic校验，规范参数）
    ark_api_key: Optional[str] = Field(None, description="火山方舟平台api_key")
    endpoint_id: str = Field(..., description="方舟平台模型端点ID（控制台获取）")
    model: Optional[str] = Field(None, description="方舟模型名称")
    api_base: str = Field("https://ark.cn-beijing.volces.com/api/v3/chat/completions", description="方舟平台基础接口地址")
    temperature: float = Field(0.7, description="采样温度")
    max_tokens: Optional[int] = Field(None, description="最大生成token数")

    @model_validator(mode="after")
    def validate_ark_api_key(self) -> "VolcEngineArkChat":
        """Pydantic校验：确保ark_api_key存在（参数/环境变量二选一）"""
        # 优先取代码传入的参数，其次取环境变量VOLC_ARK_API_KEY
        self.ark_api_key = self.ark_api_key or os.getenv("ARK_API_KEY")
        self.endpoint_id = self.endpoint_id or os.getenv("ARK_ENDPOINT_ID")
        if not self.ark_api_key:
            raise ValueError(
                "Did not find ark_api_key! "
                "Please pass it as a parameter (ark_api_key=...) or "
                "set environment variable `VOLC_ARK_API_KEY`."
            )
        # Ensure API key is ASCII-safe for HTTP headers
        try:
            self.ark_api_key.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError(
                "ark_api_key contains non-ASCII characters. "
                "Please provide the actual ASCII API key (or set VOLC_ARK_API_KEY)."
            )
        return self

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """核心方法：实现消息转换、接口调用、结果解析（LangChain标准）"""
        # 1. 转换LangChain消息为方舟平台标准格式（兼容System/Human/AIMessage）
        ark_messages = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                ark_messages.append({"role": "system", "content": msg.content})
            elif isinstance(msg, HumanMessage):
                ark_messages.append({"role": "user", "content": msg.content})
            elif isinstance(msg, AIMessage):
                ark_messages.append({"role": "assistant", "content": msg.content})

        # 2. 构造方舟平台请求体（按官方API文档规范）
        request_body = {
            "model": self.model or self.endpoint_id,
            "messages": ark_messages,
            "temperature": self.temperature,
            "stream": False  # 非流式调用，适配LangChain标准invoke
        }
        if self.max_tokens:
            request_body["max_tokens"] = self.max_tokens
        if stop:
            request_body["stop"] = stop

        # 3. 构造方舟标准请求头（Bearer+ark_api_key，无需签名）
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.ark_api_key}"
        }

        # 4. 调用方舟平台API并解析结果
        timeout_seconds = int(os.getenv("ARK_TIMEOUT", "120"))
        try:
            response = requests.post(
                url=self.api_base,
                json=request_body,
                headers=headers,
                timeout=timeout_seconds
            )
            response.raise_for_status()  # 抛出HTTP错误（如401/403）
            # Try to parse JSON; if decoding fails, include raw text for debugging
            try:
                result = response.json()
            except Exception:
                # Ensure we decode with utf-8 fallback
                content_text = None
                try:
                    content_text = response.content.decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                except Exception:
                    content_text = str(response.content)
                raise RuntimeError(
                    f"Non-JSON response from API (status={response.status_code}): {content_text}"
                )
            # 提取模型回复内容
            answer = result["choices"][0]["message"]["content"].strip()
        except Exception as e:
            # Provide richer error message to help diagnose encoding/header issues
            import traceback

            tb = traceback.format_exc()
            resp_info = None
            if "response" in locals():
                try:
                    resp_text = response.content.decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                except Exception:
                    resp_text = str(getattr(response, "content", None))
                resp_info = {
                    "status_code": getattr(response, "status_code", None),
                    "body": resp_text,
                }
            raise RuntimeError(
                "调用火山方舟API失败: "
                f"{type(e).__name__}: {repr(e)}; response={resp_info}; traceback={tb}"
            ) from e

        # 5. 转换为LangChain标准ChatResult（保证调用逻辑一致）
        chat_generation = ChatGeneration(message=AIMessage(content=answer))
        return ChatResult(generations=[chat_generation])

    @property
    def _llm_type(self) -> str:
        """标识LLM类型（LangChain标准，用于日志/回调）"""
        return "volc-engine-ark-chat"

# ------------------- 测试使用 -------------------
if __name__ == "__main__":
    # 初始化自定义方舟类：传入ark_api_key（支持参数/环境变量两种方式）
    api_key     = os.getenv("ARK_API_KEY")
    endpoint_id = os.getenv("ARK_ENDPOINT_ID", "ep-20260325170723-znh7n")
    model       = os.getenv("ARK_MODEL", "doubao-seed-2-0-lite-260215")
    chat_model = VolcEngineArkChat(
        ark_api_key=api_key,  # 直接传参（推荐生产用环境变量）
        endpoint_id=endpoint_id,
        model=model,
        temperature=0.5
    )

    # 标准LangChain消息调用逻辑（与VolcEngineMaasChat完全一致）
    messages = [
        SystemMessage(content="你是一个专业的智能助手，回答准确简洁"),
        HumanMessage(content="你好，告诉我你是那个模型，我要确认我调用的模型名字")
    ]

    # 调用模型并输出结果
    response = chat_model.invoke(messages)
    print("方舟模型回复：\n", response.content)
