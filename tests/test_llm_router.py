"""Tests for llm/router.py — 模型路由：方向（key+url+model）注册与 request_type 路由。"""

import pytest

from config import Config
from llm.analysis import SleepAnalysisLLM
from llm.ark_chat import VolcEngineArkChat
from llm.router import AnthropicCompatibleChat, LLMRoute, ModelRouter, OpenAICompatibleChat


@pytest.fixture
def clean_env(monkeypatch):
  """清空 LLM 相关环境变量，保证用例确定性。"""
  for var in ("ARK_API_KEY", "KIMI_API_KEY", "ARK_MODEL", "KIMI_MODEL", "KIMI_API_BASE"):
    monkeypatch.delenv(var, raising=False)
  return monkeypatch


def test_default_route_goes_to_volc_ark(clean_env):
  clean_env.setenv("ARK_API_KEY", "ark-key")
  clean_env.setattr(Config, "LLM_ROUTING", {"default": "volc_ark"})
  router = ModelRouter.from_env()
  route = router.route_for("analysis_explore")
  assert route.name == "volc_ark"
  assert route.api_base == Config.ARK_API_BASE
  assert isinstance(router.chat_model_for("analysis_explore"), VolcEngineArkChat)


def test_kimi_route_uses_config_url_and_env_key(clean_env):
  clean_env.setenv("ARK_API_KEY", "ark-key")
  clean_env.setenv("KIMI_API_KEY", "kimi-key")
  clean_env.setattr(Config, "LLM_ROUTING", {"analysis_explore": "kimi", "default": "volc_ark"})
  router = ModelRouter.from_env()

  route = router.route_for("analysis_explore")
  assert route.name == "kimi"
  # url 来自配置文件，key 来自环境变量
  assert route.api_base == Config.KIMI_API_BASE
  assert route.api_key == "kimi-key"
  assert route.model == Config.KIMI_MODEL

  model = router.chat_model_for("analysis_explore")
  assert isinstance(model, AnthropicCompatibleChat)  # kimi 编程订阅端点是 Anthropic 协议
  assert model.api_base == Config.KIMI_API_BASE

  # 未命中规则的 request_type 走 default
  assert router.route_for("analysis_overview").name == "volc_ark"


def test_kimi_model_env_override(clean_env):
  clean_env.setenv("KIMI_API_KEY", "kimi-key")
  clean_env.setenv("KIMI_MODEL", "kimi-k3-test")
  router = ModelRouter.from_env()
  assert router.route_for("default").model == "kimi-k3-test"


def test_fallback_when_routed_direction_missing_key(clean_env):
  # 规则指向 kimi，但 KIMI_API_KEY 未配 → 降级到可用的 volc_ark
  clean_env.setenv("ARK_API_KEY", "ark-key")
  clean_env.setattr(Config, "LLM_ROUTING", {"analysis_explore": "kimi", "default": "kimi"})
  router = ModelRouter.from_env()
  assert router.route_for("analysis_explore").name == "volc_ark"


def test_no_available_route_returns_none(clean_env):
  clean_env.setattr(Config, "LLM_ROUTING", {"default": "volc_ark"})
  router = ModelRouter.from_env()
  assert router.route_for("analysis_explore") is None
  assert router.chat_model_for("analysis_explore") is None

  llm = SleepAnalysisLLM(router=router)
  assert llm.enabled is False
  assert llm.generate_sync("analysis_overview", {}, "en", []) is None


def test_chat_model_cached_per_route_and_temperature(clean_env):
  clean_env.setenv("ARK_API_KEY", "ark-key")
  router = ModelRouter.from_env()
  m1 = router.chat_model_for("analysis_overview")
  m2 = router.chat_model_for("analysis_explore")  # 同方向同温度 → 复用
  assert m1 is m2
  m3 = router.chat_model_for("sleep_reco", temperature=0.3)  # 不同温度 → 新实例
  assert m3 is not m1
  assert m3.temperature == 0.3


def test_analysis_llm_routes_per_request_type(clean_env):
  clean_env.setenv("ARK_API_KEY", "ark-key")
  clean_env.setenv("KIMI_API_KEY", "kimi-key")
  clean_env.setattr(Config, "LLM_ROUTING", {"analysis_explore": "kimi", "default": "volc_ark"})
  llm = SleepAnalysisLLM()
  assert llm.enabled
  assert isinstance(llm._model_for("analysis_explore"), AnthropicCompatibleChat)
  assert isinstance(llm._model_for("analysis_sleep_day"), VolcEngineArkChat)
