# CODEBUDDY.md
This file provides guidance to CodeBuddy when working with code in this repository.

## Project Overview

HP Scan Tool v3.3 — a Windows desktop scanning client for HP network printers. Uses eSCL/AirScan protocol for driverless network scanning, WIA as USB fallback, and WS-Discovery for device discovery. Built with CustomTkinter, packaged as single-file EXE via PyInstaller.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run GUI (development)
python hp_scan_gui.py

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
hp_scan_gui.py (GUI Layer — CustomTkinter)
  ├── ScanApp            — Main window: scanner management + params + scan trigger
  ├── PreviewDialog      — Single-page preview + filename confirm + save
  ├── MultiPagePreviewDialog — Multi-page: thumbnails + grouping + batch save
  ├── ScannerCard        — Scanner card component (select/rename/reorder)
  └── HistoryDialog      — Scan history browser

scan_coordinator.py (Business Layer — tkinter-independent)
  └── ScanCoordinator    — Config persistence / scanner discovery / scan execution / cache coordination

escl_engine.py (Protocol Layer)  — eSCL/AirScan: discover/probe/capabilities/scan
wia_engine.py (Protocol Layer)   — WIA USB fallback
wsd_engine.py (Discovery Layer)  — WS-Discovery parallel to mDNS

cache_manager.py (I/O Layer)     — Disk cache with FIFO eviction + active job protection
history_manager.py (I/O Layer)   — JSON scan history log
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

All IO operations run in background daemon threads. UI updates via `self.after(0, callback)`. Scanner list protected by `_scanners_lock`. Config writes are atomic (`.tmp` + `os.replace`).

### Key Design Decisions

- **Disk cache over memory**: Scan data written to `{Documents}/HP_Scans/.cache/{job_id}/`, thumbnails loaded for preview, cache cleared after save
- **NextDocument polling**: Instead of polling Job URI (which returns 404 on M232 models), directly poll NextDocument endpoint — compatible with all tested HP models
- **Dual discovery**: mDNS (zeroconf) + WSD (WS-Discovery) run in parallel, results merged by IP deduplication
- **Platform-aware paths**: `sys._MEIPASS` for frozen EXE resources, `sys.executable` dir for config (avoids PyInstaller temp dir cleanup)
- **Config atomicity**: All JSON writes use `.tmp` + `os.replace` to prevent corruption

### Deprecated/Stripped Code

`exposure.py` and `preset_manager.py` are **no longer imported** by any active module. They were stripped in v3.3 (2026-07-19) and moved to `_docfix_reserve/` for a separate DocFix project. Do not reference them in new code.

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

All config files live in the EXE's directory (not `__file__` dir) for PyInstaller compatibility.

## Tech Stack

Python 3.11+ / CustomTkinter 6.0 / Pillow / python-zeroconf / WSDiscovery / requests / pywin32 (WIA) / PyInstaller 6.0

## Testing

pytest-based. `conftest.py` provides fixtures for isolated cache/config directories via `tmp_path` + `monkeypatch`. Tests cover: cache_manager, history_manager, CLI argument parsing.
