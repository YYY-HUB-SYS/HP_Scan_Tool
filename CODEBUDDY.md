# CODEBUDDY.md
This file provides guidance to CodeBuddy when working with code in this repository.

## Project Overview

HP Scan Tool v4.0 — a Windows desktop scanning client for HP network printers. Uses eSCL/AirScan protocol for driverless network scanning, WIA as USB fallback, and WS-Discovery for device discovery. Built with PySide6, packaged as single-file EXE via PyInstaller.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run GUI (development)
python hp_scan_gui_pyside.py

# Run CLI
python cli.py scan --ip 192.168.1.100 --resolution 300 --format pdf
python cli.py discover --format json
python cli.py status --ip 192.168.1.100
python cli.py history --limit 10

# Build EXE
python build_exe.py

# Run all tests
pytest tests/ -v

# Run single test file
pytest tests/test_cache_manager.py -v

# Run single test
pytest tests/test_cli.py::test_cmd_discover -v
```

## Architecture

### Layered Design

```
hp_scan_gui_pyside.py (GUI Layer — PySide6)
  ├── ScanApp            — Main window: scanner management + params + scan trigger
  ├── LoadingDialog      — Startup progress dialog
  ├── ScanWorker         — QThread for single-page scan
  ├── MultiPageScanWorker — QThread for multi-page ADF scan
  ├── ScannerCard        — Scanner card component (select/rename/reorder/disable)
  ├── ImageViewer        — Image preview with zoom/rotate
  ├── AnimatedButton     — Button with press animation
  ├── PreviewDialog      — Single-page preview + filename confirm + save
  ├── MultiPagePreviewDialog — Multi-page: thumbnails + grouping + batch save
  └── HistoryDialog      — Scan history browser

scan_coordinator.py (Business Layer — GUI-framework-independent)
  └── ScanCoordinator    — Config persistence / scanner discovery / scan execution / cache coordination

escl_engine.py (Protocol Layer)  — eSCL/AirScan: discover/probe/capabilities/scan
wia_engine.py (Protocol Layer)   — WIA USB fallback
wsd_engine.py (Discovery Layer)  — WS-Discovery parallel to mDNS
listen_engine.py (Listen Layer)  — Wait for printer panel trigger

cache_manager.py (I/O Layer)     — Disk cache with FIFO eviction + active job protection
history_manager.py (I/O Layer)   — JSON scan history log
profile_manager.py (I/O Layer)   — Per-device scan parameter profiles
cli.py (CLI Entry)               — argparse subcommands: scan/discover/status/history
```

### Data Flow

```
Scanner → eSCL/WIA engine → raw bytes → cache_manager writes to disk
                                          ↓
                                  PreviewDialog loads from cache
                                          ↓
                                  User confirms → save file → open folder
                                          ↓
                                  cache_manager cleans up cache
```

### Threading Model

All IO operations run in background daemon threads. UI updates via `QTimer.singleShot(0, callback)`. Scanner list protected by `_scanners_lock`. Config writes are atomic (`.tmp` + `os.replace`).

Scan operations use QThread workers (`ScanWorker`, `MultiPageScanWorker`) with Signal-based communication to main thread.

### Key Design Decisions

- **Disk cache over memory**: Scan data written to `{Documents}/HP_Scans/.cache/{job_id}/`, thumbnails loaded for preview, cache cleared after save
- **NextDocument polling**: Instead of polling Job URI (which returns 404 on M232 models), directly poll NextDocument endpoint — compatible with all tested HP models
- **Dual discovery**: mDNS (zeroconf) + WSD (WS-Discovery) run in parallel, results merged by IP deduplication
- **Platform-aware paths**: `sys._MEIPASS` for frozen EXE resources, `resources/` dir for development
- **Config atomicity**: All JSON writes use `.tmp` + `os.replace` to prevent corruption

### Resource Structure

```
resources/
├── fonts/
│   └── NotoSansSC-Regular.ttf    # Embedded fallback font
├── styles/
│   └── app.qss                   # Qt stylesheet
└── icons/
    ├── app.ico                   # Application icon
    ├── icon_16.png               # Taskbar icon 16px
    ├── icon_32.png               # Taskbar icon 32px
    └── icon_64.png               # Taskbar icon 64px
```

Resource path resolution: `get_resource_path()` adds `resources/` prefix in development mode, uses `sys._MEIPASS` in frozen mode.

### Reinstated Modules (`exposure.py` / `preset_manager.py`)

> ⚠️ **2026-10-21 订正**：本节标题原为 `Deprecated/Stripped Code` —— 那是 **v3.3（2026-07-19）** 的状态，
> 标题本身成了**过期句**，会让人把这两个文件当死代码。内容早已订正，**这次把标题也一并改掉**。

`exposure.py` and `preset_manager.py` are **active again** as of 2026-10-20. They were stripped in v3.3 (2026-07-19) and moved to a separate DocFix project, but that standalone DocFix software was abandoned and the basic exposure/filter features were **merged back into this project** (ADR-0008 + `docs/design/2026-10-20_新功能调研与方案_双面_续扫_纠偏_曝光_v1.0.md`). `hp_scan_gui_pyside.py` imports `preset_manager` / `exposure` for the preview exposure panel, and `save_manager.py` applies `apply_exposure` on the save path (曝光 → 旋转 → A4 归一). Presets persist at `%APPDATA%\HP_Scan_Tool\presets.json`. **Do not treat them as dead code.**

★ **2026-10-21 再订正（保存路径现状）**：`jpg` / `png` / `tiff` 产物**一律也过 `_a4_canvas`** ⇒
同一页的**图片**与 **PDF** 产物尺寸必然一致（2480×3507 @300dpi）。原来只说 PDF 过 A4 归一，已过期。

The `_docfix_reserve/` directory no longer exists (removed long ago; it is not in the repo).

### ScannerInfo Dataclass

Core data structure carrying scanner identity + capabilities. Created during discovery, enriched by `fetch_capabilities()`. The `custom_name` field is GUI-only (not serialized); persistence uses `nicknames` dict in config.

### Cache Eviction Strategy

- Configurable limit (default 1024MB), 95% trigger threshold
- FIFO by job directory creation time, whole-directory deletion
- Active jobs (registered via `register_job()`) are protected from eviction
- Startup cleanup of caches older than 24 hours

## Configuration Files

- `scan_config.json` — User preferences (theme, resolution, format, output dir, saved IPs, nicknames, selected scanner)
- `cache_config.json` — Cache tuning (limit, trigger ratio, cleanup size, stale hours)
- `history.json` — Scan history log (auto-managed, max 500 entries)
- `profiles.json` — Per-device scan parameter profiles

All config files live in `%APPDATA%\HP_Scan_Tool\` (Windows standard app data directory). This ensures the EXE is fully portable — copy it anywhere and configs follow the user profile.

## Tech Stack

Python 3.11+ / PySide6 6.5+ / Pillow / python-zeroconf / WSDiscovery / requests / pywin32 (WIA) / PyInstaller 6.0

## Testing

pytest-based. `conftest.py` provides fixtures for isolated cache/config directories via `tmp_path` + `monkeypatch`. Tests cover: cache_manager, history_manager, CLI argument parsing.
