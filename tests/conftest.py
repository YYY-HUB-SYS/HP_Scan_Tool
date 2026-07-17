"""
pytest 共享 fixtures — HP Scan Tool v3.3 测试基础设施
"""

import os
import sys
import shutil
import tempfile

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture
def tmp_cache_dir(tmp_path):
    """提供一个临时缓存目录，测试结束后自动清理"""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return str(cache_dir)


@pytest.fixture
def tmp_config_dir(tmp_path, monkeypatch):
    """将配置文件重定向到临时目录，避免污染真实配置"""
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # monkeypatch 各模块的路径函数
    import cache_manager
    import preset_manager
    import history_manager

    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: str(config_dir / "cache_config.json"))
    monkeypatch.setattr(preset_manager, "_presets_path",
                        lambda: str(config_dir / "presets.json"))
    monkeypatch.setattr(history_manager, "_history_file",
                        lambda: str(config_dir / "history.json"))

    # 重置 cache_manager 的配置缓存
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)

    return str(config_dir)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """完全隔离的缓存环境：临时目录 + 重置模块状态"""
    import cache_manager

    cache_root = str(tmp_path / "isolated_cache")
    os.makedirs(cache_root, exist_ok=True)

    # 重定向缓存根目录和配置文件路径
    monkeypatch.setattr(cache_manager, "_default_cache_root", lambda: cache_root)
    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: os.path.join(cache_root, "cache_config.json"))
    # 重置配置缓存和活跃任务
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(cache_manager, "_active_jobs", set())

    return cache_root
