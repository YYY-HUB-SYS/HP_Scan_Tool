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
wia_engine.py           # WIA 降级引擎（本地 USB 降级）
wsd_engine.py           # WS-Discovery 引擎（UDP 3702 发现 / 5357 元数据）
listen_engine.py        # 监听模式（等待面板触发）
scan_coordinator.py     # 扫描编排器（业务逻辑层）
save_manager.py         # 落盘（多页 PDF 合成 / A4 归一 / 曝光 / 纠偏施加）
exposure.py             # 曝光后处理（auto / manual）
skew_detect.py          # 倾斜角检测引擎（纯 PIL 投影法；✅ 已接入扫描流水线，随 EXE 交付）
cache_manager.py        # 磁盘缓存管理
history_manager.py      # 扫描历史日志
profile_manager.py      # 按设备保存扫描参数
preset_manager.py       # 曝光 / 扫描预设（内置 4 个 + 用户自定义）
page_meta.py            # 每页元数据（页码 / 尺寸 / 来源 / 纠偏角）
config_io.py            # 配置 I/O 共享层（原子写 + 跨进程锁）
cli.py                  # CLI 命令行入口
build_exe.py            # PyInstaller 打包脚本
tools/verify_scan.py    # 真机扫描验证脚本（产出 report.json）
tests/                  # pytest 测试套件
```

## 🛠️ 技术栈

- Python 3.11+
- PySide6 6.5+
- Pillow
- python-zeroconf
- WSDiscovery
- requests
- urllib3
- pywin32（WIA）
- pypdf（多页 PDF 合成）

## ⚙️ 配置文件

- `scan_config.json` — 用户偏好（分辨率、颜色模式、格式、输出目录、已保存打印机、纠偏阈值）
- `cache_config.json` — 缓存策略（上限、触发线、清理量）
- `history.json` — 扫描历史日志（最多 500 条）
- `profiles.json` — 按设备保存的扫描参数
- `presets.json` — 用户自定义曝光 / 扫描预设

所有配置文件位于 `%APPDATA%\HP_Scan_Tool\`。

## 🧪 测试

```bash
pytest tests/ -v
```

## ⚠️ 已知限制

- HTTPS 端口返回 301 时**已跟随重定向**（http 目标直接跟、https 目标走宽松 TLS 重试）
- 预览为缩略图，非全分辨率原图查看
- 单文件 EXE **65,866,395 字节（约 62.8 MB）** —— `dist/HP_Scan_Tool.exe` 实测值（LastWriteTime `2026-10-21 01:30`，`19efd5c` 排除 numpy 之后重新打出的包）
- 当前版本无自动更新机制
- GUI 界面**没有主题切换**（v4.0 单主题）、**没有纠偏开关**（纠偏阈值只能用配置里的默认值）
