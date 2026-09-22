# 🖨️ HP Scan Tool

惠普集成扫描工具（HP Integrated Scan Tool）是一款面向惠普网络打印机的 Windows 桌面扫描客户端。通过 eSCL/AirScan 协议实现免驱网络扫描，WIA 作为本地 USB 降级方案，WS-Discovery 作为补充发现协议。

## 功能特性

- 🚀 **免驱扫描**：eSCL/AirScan 纯 HTTP/XML 协议，无需安装厂商驱动
- 🔍 **双协议发现**：mDNS + WS-Discovery 并行发现局域网扫描仪
- 📄 **多页扫描**：支持平板单页和 ADF 自动多页扫描
- 💾 **磁盘缓存**：扫描数据先落盘，预览后确认保存，避免内存占用
- 📝 **历史记录**：自动记录扫描历史，支持浏览和清空
- 💻 **CLI 模式**：支持命令行批量扫描/发现/状态查询
- 📦 **单文件 EXE**：PyInstaller 打包，U 盘即走

## 🚀 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 开发模式运行 GUI
python hp_scan_gui_pyside.py

# CLI 模式扫描
python cli.py scan --ip 192.168.1.100 --resolution 300 --format pdf

# 发现扫描仪
python cli.py discover --format json

# 查看扫描历史
python cli.py history --limit 10
```

## 📦 打包

```bash
python build_exe.py
```

输出：`dist/HP_Scan_Tool.exe`（单文件便携版）

## 🗂️ 项目结构

```
hp_scan_gui_pyside.py   # GUI 主程序（PySide6）
escl_engine.py          # eSCL/AirScan 协议引擎
wia_engine.py           # WIA 降级引擎
wsd_engine.py           # WS-Discovery 引擎
listen_engine.py        # 监听模式（等待面板触发）
scan_coordinator.py     # 扫描编排器（业务逻辑层）
cache_manager.py        # 磁盘缓存管理
history_manager.py      # 扫描历史日志
profile_manager.py      # 按设备保存扫描参数
cli.py                  # CLI 命令行入口
build_exe.py            # PyInstaller 打包脚本
```

## 🛠️ 技术栈

- Python 3.11+
- PySide6 6.5+
- Pillow
- python-zeroconf
- WSDiscovery
- requests
- pywin32（WIA）

## ⚙️ 配置文件

- `scan_config.json` — 用户偏好（主题、分辨率、格式、输出目录、已保存打印机）
- `cache_config.json` — 缓存策略（上限、触发线、清理量）
- `history.json` — 扫描历史日志（最多 500 条）
- `profiles.json` — 按设备保存的扫描参数

所有配置文件位于 `%APPDATA%\HP_Scan_Tool\`。

## 🧪 测试

```bash
pytest tests/ -v
```

## ⚠️ 已知限制

- M232 等机型 HTTPS 端口返回 301 时暂不跟随重定向
- 预览为缩略图，非全分辨率原图查看
- 单文件 EXE 约 22MB
- 当前版本无自动更新机制
