r"""
pytest 共享 fixtures — HP Scan Tool v4.0 测试基础设施

★ 用户数据隔离（2026-10-21 补）
------------------------------------------------------------------
**背景**：核销过程中发现 `tests/conftest.py` 的 `isolated_cache` fixture
**没有重定向 `history_manager._history_file`** ⇒ 用它的集成测试把记录写进了
**真实的** `%APPDATA%\HP_Scan_Tool\history.json`。
实测真实历史 **70 条记录里 64 条是 pytest 污染**（且仍在持续发生）。
这个坑本项目早期就踩过一次，当时只清理了数据、**没修根因**。

**两道防线**（缺一不可）：

1. **导入期**（本文件模块级）：把 `APPDATA` 指向本次 pytest 会话专属的沙箱目录。
   必须在这一层做，是因为各模块解析用户目录的**时机不一致**：
   - `history_manager._app_dir = config_dir()`  ⇒ **导入时求值**
   - `profile_manager._app_dir` ⇒ 首次调用时惰性求值
   - `scan_coordinator._app_dir()` / `preset_manager._presets_path()` ⇒ **每次调用时**读 `APPDATA`
   只有在 conftest 导入阶段就改环境变量，三种时机才都被拦住。

2. **每测试期**（autouse fixture）：把每个模块的路径函数逐一重定向到该测试的
   `tmp_path`，并**断言**解析结果绝不落在真实用户目录下 —— 断言失败就是
   "某个 fixture 又把数据写到用户目录了"，直接让测试红掉，而不是静默污染。
"""

import os
import sys
import shutil
import tempfile

import pytest

# 必须在**任何** application 实例被创建之前设置：禁止弹出真实窗口。
# 放在 conftest（而不是各测试文件里）⇒ 比模块级 import 更早、更统一。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── 防线 1：导入期把 APPDATA 指向会话沙箱 ────────────────────────────────
# 必须在任何被测模块被 import 之前执行（conftest.py 由 pytest 先行导入）。
_SESSION_APPDATA = tempfile.mkdtemp(prefix="hp_scan_tool_pytest_")
os.environ["APPDATA"] = _SESSION_APPDATA

# 真实用户目录（仅用于断言比对，不做任何写操作）
_REAL_USER_DIR = os.path.join(
    os.path.expanduser("~"), "AppData", "Roaming", "HP_Scan_Tool")


@pytest.fixture(scope="session", autouse=True)
def _cleanup_session_appdata():
    """会话结束后清掉沙箱 APPDATA 目录"""
    yield
    shutil.rmtree(_SESSION_APPDATA, ignore_errors=True)


# ── Qt application 唯一性（会话级，全项目共用一个） ────────────────────────

@pytest.fixture(scope="session")
def qt_app():
    """
    整个 pytest 进程**唯一**的 Qt application 实例（QApplication，offscreen）。

    ★ 为什么必须统一在这里建（2026-10-21 的 0xC0000409 崩溃根因）
    ------------------------------------------------------------------
    实测事实（探针 `%TEMP%\\hp_qapp_probe2.py`，非推断）：

    - 进程里只有 **QCoreApplication** 时，`QApplication.instance()`
      返回的**就是那个 QCoreApplication**（**非 None**），
      且 `isinstance(x, QApplication)` 为 **False**。
    - `del` 掉 Python 引用后 `QCoreApplication.instance()` **依然返回它**
      ⇒ PySide6 一直持有该实例 ⇒ function 级 fixture 根本"析构"不掉。

    于是旧写法 `QApplication.instance() or QApplication([])` 会**短路**
    ⇒ QApplication **从未被创建** ⇒ 第一个 widget（QCheckBox）创建即
    `QWidget: Cannot create a QWidget without QApplication` ⇒ 进程
    `0xC0000409` 硬崩（pytest 连 traceback 都来不及打）。

    ★ 契约
      - 全项目**只有**本 fixture 能创建 application 实例。
      - 任何测试文件都不得自己 `QCoreApplication(...)` / `QApplication(...)`。
      - 若进程里已存在 QCoreApplication，这里**立刻**抛可读错误，
        而不是让后续某个 widget 静默崩溃。
    """
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if not isinstance(app, QApplication):
        core = QCoreApplication.instance()
        if core is not None:
            raise RuntimeError(
                "进程里已存在 QCoreApplication（%s）—— QCoreApplication 与 "
                "QApplication 不能混用。请改用 tests/conftest.py 的会话级 "
                "qt_app fixture，不要自己 new application 实例。"
                % type(core).__name__)
        app = QApplication([])
    return app


@pytest.fixture(autouse=True)
def _isolate_user_data(tmp_path, monkeypatch):
    """
    ★ 防线 2：逐测试把用户数据隔离到 tmp_path，并断言不落真实目录。

    ★ 关键设计：**只改环境变量与模块级缓存，不 monkeypatch 任何路径函数**。

    早先版本把 `config_io.config_dir` / `preset_manager._presets_path` /
    `scan_coordinator._app_dir` 等路径函数整个替换掉，结果**把被测函数屏蔽了** ——
    例如 `test_config_dir_uses_appdata` / `test_path_stays_in_appdata` 这两条
    本来要验"路径确实从 APPDATA 派生"，被替换后只能看到沙箱路径，断言反而失败。
    ⇒ 隔离必须靠**环境变量**（所有模块最终都从 `APPDATA` 派生），路径函数保持原样。

    覆盖的解析路径：
      config_io.config_dir()          → 每次调用读 APPDATA       ✔ 环境变量即可
      preset_manager._presets_path()  → 每次调用读 APPDATA       ✔ 环境变量即可
      scan_coordinator._app_dir()     → 每次调用读 APPDATA       ✔ 环境变量即可
      cache_manager._app_dir()        → 每次调用读 APPDATA       ✔ 环境变量即可
      history_manager._get_app_dir()  → **惰性缓存** _app_dir     ⇒ 需显式指向本次目录
      profile_manager._get_app_dir()  → **惰性缓存** _app_dir     ⇒ 需显式指向本次目录
    """
    import cache_manager
    import config_io
    import history_manager
    import preset_manager
    import profile_manager
    import scan_coordinator

    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    sb = str(appdata)

    # ① 环境变量：覆盖所有"每次调用读 APPDATA"的模块
    monkeypatch.setenv("APPDATA", sb)

    # ② 模块级缓存：history_manager / profile_manager 把目录缓存成模块变量
    #    raising=False：被测模块结构可能变化，属性不存在时不该让所有测试炸掉
    monkeypatch.setattr(history_manager, "_app_dir", sb, raising=False)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None, raising=False)
    monkeypatch.setattr(profile_manager, "_app_dir", sb, raising=False)

    # ③ 断言：解析出来的路径绝不落在真实用户目录内
    real = os.path.abspath(_REAL_USER_DIR)
    probes = [
        ("config_io.config_dir", config_io.config_dir),
        ("history_manager._history_file", history_manager._history_file),
        ("preset_manager._presets_path", preset_manager._presets_path),
        ("profile_manager._profile_path", profile_manager._profile_path),
        ("cache_manager._cache_config_path", cache_manager._cache_config_path),
        ("scan_coordinator._app_dir", scan_coordinator._app_dir),
    ]
    for name, fn in probes:
        try:
            resolved = os.path.abspath(fn())
        except Exception:          # 路径解析本身抛错不是护栏要管的事
            continue
        assert not resolved.startswith(real), (
            "测试正在写真实用户数据目录！%s -> %s（真实目录 %s）"
            % (name, resolved, real))


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
    import history_manager
    import preset_manager

    monkeypatch.setattr(cache_manager, "_cache_config_path",
                        lambda: str(config_dir / "cache_config.json"))
    monkeypatch.setattr(history_manager, "_history_file",
                        lambda: str(config_dir / "history.json"))
    # 曝光预设默认落在 %APPDATA%\\HP_Scan_Tool\\presets.json，测试期重定向到临时目录
    monkeypatch.setattr(preset_manager, "_presets_path",
                        lambda: str(config_dir / "presets.json"))

    # 重置 cache_manager 的配置缓存和 history_manager 的路径缓存
    monkeypatch.setattr(cache_manager, "_config_cache", None)
    monkeypatch.setattr(cache_manager, "_config_cache_time", 0)
    monkeypatch.setattr(history_manager, "HISTORY_FILE", None)

    return str(config_dir)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """
    完全隔离的缓存环境：临时目录 + 重置模块状态。

    ★ 注意：autouse 的 `_isolate_user_data` 已把**用户数据**路径全部重定向；
    这里额外隔离**缓存根目录**（缓存里放的是扫描页，不是用户配置）。
    """
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
    # 重置"已扫描未保存"保护集合与缓存大小增量计数（换根目录后旧计数无效）
    monkeypatch.setattr(cache_manager, "_pending_jobs", set())
    monkeypatch.setattr(cache_manager, "_cache_size_cache", None)

    return cache_root
