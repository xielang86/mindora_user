"""llm — Mindora LLM 能力包。

公共出口（调用方只从这里 import，内部文件拆分不影响外部）：
  - VolcEngineArkChat      火山方舟 LangChain 适配器（ark_chat.py）
  - OpenAICompatibleChat   OpenAI 兼容客户端，Kimi/Moonshot 等（router.py）
  - ModelRouter / LLMRoute 模型路由：一组 key(env)+url(config) 是一个请求方向（router.py）
  - SleepAnalysisLLM       /analysis 文案生成（analysis.py）
  - extract_sleep_context  画像 → LLM 上下文聚合（analysis.py）
  - deep_merge             LLM 文案深合并进响应骨架（analysis.py）
  - RecommendationEngine   睡眠场景/SOP 推荐（reco.py）
"""

from llm.ark_chat import VolcEngineArkChat
from llm.router import LLMRoute, ModelRouter, OpenAICompatibleChat
from llm.analysis import SleepAnalysisLLM, deep_merge, extract_sleep_context
from llm.reco import RecommendationEngine

__all__ = [
    "VolcEngineArkChat",
    "OpenAICompatibleChat",
    "ModelRouter",
    "LLMRoute",
    "SleepAnalysisLLM",
    "extract_sleep_context",
    "deep_merge",
    "RecommendationEngine",
]
