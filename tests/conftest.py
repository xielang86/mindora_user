"""Shared fixtures for mindora_user tests."""
import sys, os, pytest

# Ensure mindora_user package root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# tests/ 自身也可被导入：测试间共享的夹具模块（如 popup_config_fixture）
sys.path.insert(0, os.path.dirname(__file__))


def pytest_configure(config):
    """Override asyncio mode to 'auto' so async fixtures work without
    extra decorators."""
    config.option.asyncio_mode = "auto"
