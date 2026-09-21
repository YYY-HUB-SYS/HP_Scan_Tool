---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 130e5aeed5b74cecf6b1d4047ad973d4_38bdfb26810511f1a60e525400e6dd8f
    ReservedCode1: wx4WYcK0T51YBi3QpdzeruDGDHqNqZrKjopHEwHMHPoubz/MJHQtEiyj+LOLVV8Eeyb1xLa3pZbOr2M2m8mYXB4Q4KeUN9jTGhUzrM4HQ3kQa8r1Nx9G88+0SPauJ8b2fPF5Q/BPh8BrQDUkz9SYc4h9j7K3N8KgQhMp8mj+O9KiFvezyTjAhKdmRVc=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 130e5aeed5b74cecf6b1d4047ad973d4_38bdfb26810511f1a60e525400e6dd8f
    ReservedCode2: wx4WYcK0T51YBi3QpdzeruDGDHqNqZrKjopHEwHMHPoubz/MJHQtEiyj+LOLVV8Eeyb1xLa3pZbOr2M2m8mYXB4Q4KeUN9jTGhUzrM4HQ3kQa8r1Nx9G88+0SPauJ8b2fPF5Q/BPh8BrQDUkz9SYc4h9j7K3N8KgQhMp8mj+O9KiFvezyTjAhKdmRVc=
---



# 惠普集成扫描工具 — 产品需求文档 (PRD)

> 版本: v3.1 | 日期: 2026-07-16 | 作者: Marvis

---

## 目录

1. [产品概述](#1-产品概述)
2. [功能需求](#2-功能需求)
3. [技术架构](#3-技术架构)
4. [eSCL 协议详解](#4-escl-协议详解)
5. [数据结构](#5-数据结构)
6. [配置文件规范](#6-配置文件规范)
7. [UI 规范](#7-ui-规范)
8. [错误处理矩阵](#8-错误处理矩阵)
9. [打印机兼容性矩阵](#9-打印机兼容性矩阵)
10. [构建与分发](#10-构建与分发)
11. [待修复项](#11-待修复项)
12. [Bug 修复历史](#12-bug-修复历史)
13. [已知限制](#13-已知限制)
14. [测试场景](#14-测试场景)
15. [未来规划](#15-未来规划)

---

## 1. 产品概述

### 1.1 产品定位

惠普集成扫描工具 (HP Integrated Scan Tool) 是一款面向惠普网络打印机的 Windows 桌面扫描客户端。通过 eSCL/AirScan 协议实现免驱网络扫描，WIA 本地扫描作为降级方案。目标是让办公人员在局域网内实现"选打印机 → 点扫描 → 拿到文件"的最短路径。

### 1.2 核心价值主张

| 价值点 | 传统方案痛点 | 本工具解决方式 |
|--------|-------------|---------------|
| 免驱 | 需安装厂商数百MB驱动包 | eSCL 协议纯 HTTP/XML，零驱动 |
| 零配置 | IP 地址难记、Web 界面操作繁琐 | mDNS 自动发现 + 历史记录秒加载 |
| 现代 UI | HP Smart 臃肿、Web 界面卡顿 | CustomTkinter 本地原生窗口，深色/浅色双主题 |
| 单文件便携 | 需要安装步骤和管理员权限 | 22MB 单文件 EXE，U 盘即走 |

### 1.3 目标用户画像

- **主要用户**：局域网办公人员，日常文档/发票/合同扫描归档
- **次要用户**：IT 运维人员，巡检多台打印机 eSCL 服务状态
- **技术门槛**：无需任何网络或打印机协议知识，双击 EXE 即可使用

### 1.4 使用场景

| 场景 | 操作路径 |
|------|---------|
| 日常单页扫描 | 启动 → 已自动加载历史打印机 → 单击选中 → 点"扫描" |
| 批量扫描（ADF） | 放入输稿器 → 自动检测切换 Feeder → 扫描 |
| 首次使用 | 启动 → 自动 mDNS 搜索 → 列表出现打印机 → 选中扫描 |
| 新打印机加入 | 点"手动添加" → 输入 IP → 自动探测 eSCL |
| 更换保存位置 | 点"浏览"选目录 → 自动持久化，下次启动保留 |
| 区分同名打印机 | 右键卡片 → 自定义名称（如"三楼HP"、"财务室HP"） |

---

## 2. 功能需求

### 2.1 模块架构总览

```
┌─────────────────────────────────────────────┐
│                 hp_scan_gui.py               │
│         (CustomTkinter UI 主程序)            │
│   ┌─────────┐  ┌──────────┐  ┌───────────┐ │
│   │ 发现管理 │  │ 参数面板  │  │ 扫描调度   │ │
│   └────┬────┘  └──────────┘  └─────┬─────┘ │
└────────┼───────────────────────────┼────────┘
         │                           │
    ┌────▼─────┐              ┌──────▼──────┐
    │escl_engine│              │  wia_engine  │
    │(eSCL协议) │              │  (WIA兜底)   │
    └──────────┘              └─────────────┘
```

### 2.2 打印机发现模块

#### 2.2.1 mDNS 自动发现

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 服务类型 | `_uscan._tcp.local.`、`_scanner._tcp.local.` |
| 超时 | 4 秒 |
| 协议库 | python-zeroconf ≥ 0.39 |
| 实现文件 | `escl_engine.py::discover_scanners()` |
| 备注 | 后台线程执行，不阻塞 UI；有历史记录时跳过 |

**发现流程**：

```
启动 Zeroconf → 注册 ServiceBrowser → sleep(4s) → 收集结果 → zc.close()
                                    ↓
                          _EsclListener.add_service()
                          解析 IP/端口/mDNS 属性
                          按 IP 去重 → 追加到 found 列表
```

**mDNS 属性映射**：

| mDNS 属性键 | ScannerInfo 字段 |
|-------------|-----------------|
| `ty` 或 `mdl` | `model` |
| `sern` 或 `usb_MFG` | `serial` |
| `name`（去掉末尾 `.`） | `name` |

#### 2.2.2 eSCL 服务探测

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 实现函数 | `escl_engine.py::probe_escl(ip, port, timeout)` |
| 探测路径 | ① `/eSCL/ScannerStatus` → ② `/ScannerStatus`（根路径兜底） |
| 返回 | eSCL 基础 URL（如 `http://x.x.x.x/eSCL/`）或 None |
| 超时 | 3-4 秒 |

#### 2.2.3 手动添加

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 触发 | 点击"手动添加"按钮 |
| 流程 | 弹出对话框 → 输入 IP → 点"探测并添加" → 调用 probe_escl + fetch_capabilities → 成功则追加到列表并自动保存 |

#### 2.2.4 历史记录加载

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 数据源 | `scan_config.json` 中 `saved_ips` 数组 |
| 加载时机 | `_restore_presets()` — `__init__` 末尾同步调用 |
| 探活策略 | `_probe_saved()` — 后台逐台调用 probe_escl，每台探通后 `after(0, _rebuild_cards)` 异步刷新 UI |
| 去重 | 按 `ip` 字段去重 |

### 2.3 能力查询模块

#### 2.3.1 ScannerCapabilities 解析

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 实现函数 | `escl_engine.py::fetch_capabilities(scanner, timeout)` |
| 端点 | `GET {escl_url}/ScannerCapabilities` |
| 超时 | 5 秒 |
| 命名空间 | 支持 `imaging/escl/2011/05/03` 和 `scanner/escl/2011/09` 两种 |

**解析字段对照表**：

| XML 元素 | ScannerInfo 字段 | 默认值 |
|----------|-----------------|--------|
| `Model` | `model` | 空字符串 |
| `SerialNumber` | `serial` | 空字符串 |
| 顶层 `Platen` 子元素存在 | `has_platen = True` | True |
| 顶层 `Adf` 子元素存在 | `has_adf = True` | False |
| `Adf/Duplex` = "true" | `has_duplex = True` | False |
| `MaxWidth` | `max_width` | 2550 |
| `MaxHeight` | `max_height` | 4200 |
| `DiscreteResolutions` 或 `SupportedResolutions` 下 `Width` | `resolutions` | [75,150,200,300,600] |
| `ColorModes` 或 `SupportedColorModes` 子元素 | `color_modes` | ["RGB24","Grayscale8"] |
| `DocumentFormats` 或 `SupportedDocumentFormats` 子元素 | `formats` | ["image/jpeg","application/pdf"] |

#### 2.3.2 ScannerStatus 查询

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 实现函数 | `escl_engine.py::get_scanner_status(escl_url, timeout)` |
| 端点 | `GET {escl_url}/ScannerStatus` |
| 返回字典 | `{"state": "Idle"/"Processing"/"Unknown", "adf_loaded": True/False}` |
| 用途 | ① 预览状态按钮弹窗 ② 扫描前 ADF 自动检测 |

### 2.4 扫描执行模块

#### 2.4.1 eSCL 扫描流程

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 实现函数 | `escl_engine.py::execute_scan()` → `scan_to_file()` |
| 超时 | 120 秒（可配置，GUI 中传 90 秒） |
| 轮询间隔 | 0.5 秒 |

**完整交互序列**：

```
  客户端                          打印机 (eSCL)
    │                                │
    │── POST /eSCL/ScanJobs ────────→│  创建扫描任务
    │   Content-Type: application/xml│
    │   <scan:ScanJob>...</scan:ScanJob>
    │←── 201 Created ────────────────│
    │   Location: /eSCL/ScanJobs/xxx │
    │                                │
    │   ┌─ 轮询循环 ──────────────┐  │
    │   │ GET .../NextDocument    │  │
    │   │   ← 503 = 扫描进行中    │  │
    │   │   ← 200 = 返回图片数据  │  │  返回 JPEG/PNG/TIFF
    │   └─ 重复直到 200 或超时 ──┘  │
    │                                │
```

**关键设计决策 — 为什么轮询 NextDocument 而不是 Job URI**：

| 机型 | POST 返回 | GET job URI | GET NextDocument |
|------|----------|-------------|-----------------|
| HP 7720 | 201 | 200 + JobState XML | 200 返回数据 |
| HP M232 | 201 | **404**（永远） | 503→200 |

M232 系列 ApolloLedmServer 固件创建 ScanJob 后立刻删除 job 记录，但 NextDocument 端点持续有效（503=扫描中，200=完成）。因此统一采用 NextDocument 直接轮询方案，兼容全部已测试机型。

#### 2.4.2 扫描参数

| 参数 | 可选值 | 映射关系 |
|------|--------|---------|
| 分辨率 (resolution) | 75, 150, 200, 300, 600, 1200 DPI | 直接传递 |
| 颜色模式 (color_mode) | 彩色→RGB24, 灰度→Grayscale8, 黑白→BlackAndWhite1 | `COLOR_MODE_MAP` |
| 格式 (output_format) | jpg→image/jpeg, png→image/png, pdf→application/pdf, tiff→image/tiff | `FORMAT_MIME` |
| 来源 (source) | Platen（平板）, Feeder（输稿器） | 直接传递 |
| 双面 (duplex) | true/false | 仅 ADF 支持时可用 |

#### 2.4.3 PDF 转码

当用户选择 PDF 格式但打印机返回 JPEG 数据时（部分机型不支持原生 PDF），引擎自动用 Pillow 将 JPEG 转为 PDF：

```
execute_scan 返回 (jpeg_bytes, "jpg")
    ↓
scan_to_file 检测 output_format="pdf" && ext!="pdf"
    ↓
Image.open(BytesIO(data)).convert("RGB").save(pdf_path, "PDF")
    ↓
返回 pdf_path
```

#### 2.4.4 ADF 自动检测

扫描前自动检查输稿器状态，无需用户手动切换来源：

```
_start_scan → _do_scan
    ├── if scanner.has_adf:
    │     get_scanner_status() → adf_loaded?
    │     ├── True  → source="Feeder", 状态栏提示"输稿器检测到纸张"
    │     └── False → source="Platen", 状态栏提示"输稿器为空"
    └── scan_to_file(source=source_val)
```

#### 2.4.5 WIA 降级扫描

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 触发条件 | eSCL 不可用（escl_url 为空）时点击扫描 |
| 实现文件 | `wia_engine.py` |
| 依赖 | pywin32 (win32com) |
| 流程 | `list_wia_scanners()` → 取首个扫描仪 → `wia_scan_to_file()` |

### 2.5 配置持久化模块

#### 2.5.1 配置文件路径策略

```
def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)  # EXE 所在目录
    return os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录

CONFIG_FILE = os.path.join(_app_dir(), "scan_config.json")
```

**为什么不用 `__file__`**：PyInstaller 单文件模式下 `__file__` 指向 `%TEMP%\_MEIxxxxx\` 临时目录，每次重启被系统清理。EXE 所在目录是稳定持久位置。

#### 2.5.2 配置写入时机

| 操作 | 写入内容 | 触发函数 |
|------|---------|---------|
| 主题切换 | `theme` | `_toggle_theme` → `save_config` |
| 浏览选择目录 | `output_dir` | `_browse` → `save_config` |
| 开始扫描 | `resolution`, `color_mode_ui`, `output_format`, `output_dir` | `_start_scan` → `save_config` |
| 发现/刷新完成 | `saved_ips`, `nicknames` | `_do_discover` → `save_config` |
| 重命名打印机 | `nicknames` | `set_custom_name` → `save_config` |

### 2.6 UI 交互模块

#### 2.6.1 窗口规格

| 属性 | 值 |
|------|---|
| 默认尺寸 | 840×620 px |
| 最小尺寸 | 720×500 px |
| 标题 | "惠普集成扫描工具" |
| 布局 | 顶栏 + 左栏(扫描仪列表) + 右栏(操作面板) + 底部进度条 |

#### 2.6.2 主题系统

| 属性 | 值 |
|------|---|
| 默认主题 | 上次使用的主题（配置持久化） |
| 切换控件 | 顶栏右侧 SegmentedButton [light | dark] |
| 联动范围 | 全局 `ctk.set_appearance_mode()`，所有 CTk 控件自动适配 |
| 配色约束 | 标签全部高对比度（深色底+白字），按钮 text_color 手动适配双主题 |

#### 2.6.3 扫描仪卡片

每张卡片包含三行：

```
┌──────────────────────────────────────┐
│ HP LaserJet MFP M232-M237            │ ← 名称行（加粗 13px，wraplength=260）
│ IP x.x.x.x  |  M232-M237       │ ← 信息行（11px 灰色）
│ [eSCL] [平板] [600DPI]               │ ← 标签行（彩色圆角标签）
└──────────────────────────────────────┘
```

**标签配色方案**：

| 标签 | 背景色 | 含义 |
|------|--------|------|
| eSCL | #1b7c6e | eSCL 已探通 |
| ?eSCL | #c25a3e | eSCL 未探通 |
| ADF | #1b6e8a | 有输稿器 |
| 平板 | #3d6e8a | 仅平板 |
| 未知 | #5a5a5a | 能力未知 |
| {max}DPI | #4d5a6e | 最大分辨率 |

**交互行为**：

| 操作 | 效果 |
|------|------|
| 单击卡片 | 选中（蓝色边框 #3B8ED0/#1F6AA5，2px） |
| 双击卡片 | 直接触发扫描 |
| 右键卡片 | 弹出重命名对话框 |

#### 2.6.4 按钮状态

| 按钮 | 正常态 | 扫描中 |
|------|--------|--------|
| 搜索局域网 | normal, "搜索局域网" | disabled, "搜索中..." |
| 扫描 | normal, "扫描" | disabled, "扫描中..." |
| 查询能力 | 始终可用 | — |
| 预览状态 | 始终可用 | — |

---

## 3. 技术架构

### 3.1 技术栈明细

| 层级 | 技术 | 版本要求 | 用途 |
|------|------|---------|------|
| 语言 | Python | ≥ 3.10 | — |
| GUI | CustomTkinter | 6.0.0 | 现代扁平 UI 框架 |
| 网络发现 | python-zeroconf | ≥ 0.39 | mDNS/DNS-SD 服务发现 |
| HTTP 客户端 | requests | ≥ 2.28 | eSCL REST API 调用 |
| XML 解析 | xml.etree.ElementTree | 内置 | eSCL XML 解析 |
| 图像处理 | Pillow (PIL) | ≥ 9.0 | JPEG→PDF 转码 |
| WIA 扫描 | pywin32 | ≥ 305 | Windows WIA COM 接口 |
| 打包 | PyInstaller | ≥ 6.0 | 单文件 EXE 打包 |

### 3.2 文件结构

```
HP_Scan_Tool/
├── hp_scan_gui.py          # GUI 主程序（752 行）
│   ├── class ScanApp(ctk.CTk)     # 主窗口
│   │   ├── 发现: _restore_presets / _auto_discover / _do_discover / _probe_saved
│   │   ├── 选择: select_scanner
│   │   ├── 扫描: _start_scan / _do_scan / _wia_scan / _scan_done
│   │   ├── 能力: _query_caps / _check_status
│   │   └── 配置: load_config / save_config / _browse / set_custom_name
│   └── class ScannerCard(ctk.CTkFrame)  # 扫描仪卡片
│
├── escl_engine.py          # eSCL 协议引擎（452 行）
│   ├── ScannerInfo / ScanJob       # 数据结构
│   ├── discover_scanners()         # mDNS 发现
│   ├── probe_escl()                # eSCL 探测
│   ├── fetch_capabilities()        # 能力查询
│   ├── get_scanner_status()        # 状态查询
│   ├── execute_scan()              # 扫描执行（核心）
│   └── scan_to_file()              # 扫描+保存
│
├── wia_engine.py           # WIA 扫描引擎（169 行）
│   ├── list_wia_scanners()
│   ├── wia_scan_to_file()
│   └── try_wia_scan()
│
├── build_exe.py            # PyInstaller 打包脚本
├── requirements.txt        # pip 依赖声明
├── HP_Scan_Tool.spec       # PyInstaller spec（自动生成）
├── run.bat                 # 开发环境启动脚本
├── PRD.md                  # 本文件
└── dist/
    └── HP_Scan_Tool.exe    # 最终交付物（~22MB）
```

### 3.3 线程模型

所有 IO 操作均在后台线程执行，UI 通过 `self.after(0, callback)` 回主线程更新：

| 操作 | 线程 | 阻塞时长 |
|------|------|---------|
| mDNS 发现 | 后台 daemon | ~4s |
| eSCL 探活 | 后台 daemon | ~3s/台 |
| 能力查询 | 后台 daemon | ~5s |
| 状态查询 | 后台 daemon | ~4s |
| 扫描执行 | 后台 daemon | 最多 120s |
| UI 渲染 | 主线程 | — |

### 3.4 打包配置

```python
# build_exe.py 核心参数
"--onefile"              # 单文件 EXE
"--windowed"             # 无控制台窗口
"--name", "HP_Scan_Tool"
"--add-data", "escl_engine.py;."   # 内嵌引擎
"--add-data", "wia_engine.py;."    # 内嵌 WIA 引擎
"--hidden-import", "requests"
"--hidden-import", "zeroconf"
"--hidden-import", "customtkinter"
"--hidden-import", "PIL"
"--clean"
"--noconfirm"
```

产物大小：~22 MB（含 Python 运行时 + 全部依赖 + 资源）

---

## 4. eSCL 协议详解

### 4.1 协议概述

eSCL (eScanner Communication Language) 是 Mopria Alliance 定义的网络扫描协议，基于 HTTP/1.1 + XML。HP 以 "AirScan" 品牌实现。

### 4.2 端点列表

| 方法 | 路径 | 用途 | 响应 |
|------|------|------|------|
| GET | `/eSCL/ScannerCapabilities` | 获取扫描仪能力 | 200 + XML |
| GET | `/eSCL/ScannerStatus` | 获取当前状态 | 200 + XML |
| POST | `/eSCL/ScanJobs` | 创建扫描任务 | 201 + Location header |
| GET | `/eSCL/ScanJobs/{id}` | 查询任务状态 | 200 + XML 或 404 |
| GET | `/eSCL/ScanJobs/{id}/NextDocument` | 获取扫描结果 | 200 (图片数据) 或 503 (进行中) |

### 4.3 命名空间差异

不同机型使用不同 XML 命名空间，引擎通过 `tag(el)` 函数剥离命名空间前缀实现兼容：

```python
def tag(el):
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag
```

| 机型 | Server | 命名空间 |
|------|--------|---------|
| HP 7720 | nginx v2.62 | `imaging/escl/2011/05/03` |
| HP M232 | ApolloLedmServer v2.63 | `imaging/escl/2011/05/03` |
| HP M1216 | Marvell Mrvl-R1_0 | 不支持 eSCL |

### 4.4 POST ScanJobs XML 模板

```xml
<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanJob
    xmlns:scan="http://schemas.hp.com/scanner/escl/2011/09"
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <scan:InputSource>Platen</scan:InputSource>
  <scan:Intent>Document</scan:Intent>
  <scan:ScanRegions>
    <scan:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:Width>2550</pwg:Width>
      <pwg:Height>4200</pwg:Height>
      <scan:ScanRegionXOffset>0</scan:ScanRegionXOffset>
      <scan:ScanRegionYOffset>0</scan:ScanRegionYOffset>
    </scan:ScanRegion>
  </scan:ScanRegions>
  <scan:XResolution>300</scan:XResolution>
  <scan:YResolution>300</scan:YResolution>
  <scan:ColorMode>RGB24</scan:ColorMode>
  <scan:Duplex>false</scan:Duplex>
</scan:ScanJob>
```

**ContentRegionUnits 说明**：`escl:ThreeHundredthsOfInches`（1/300 英寸）是 eSCL 标准单位，因此 2550 = 8.5 英寸（Letter 宽度），4200 = 14 英寸（Legal 长度）。

### 4.5 轮询策略决策树

```
POST ScanJobs → 201 Created
         ↓
   获取 Location header
         ↓
   构造 NextDocument URL
         ↓
   ┌─ 轮询循环 (0.5s间隔) ─┐
   │ GET NextDocument      │
   │   ├─ 200 → 返回数据 ✅ │
   │   ├─ 503 → 继续等待    │
   │   └─ 其他 → 继续等待   │
   └───────────────────────┘
         ↓ 超时
   TimeoutError
```

---

## 5. 数据结构

### 5.1 ScannerInfo

```python
@dataclass
class ScannerInfo:
    name: str                        # mDNS 名称或 "手动添加的打印机"
    ip: str                          # IPv4 地址
    port: int = 80                   # HTTP 端口
    escl_url: str = ""               # eSCL 基础 URL，如 "http://x.x.x.x/eSCL/"
    model: str = ""                  # 型号，如 "HP LaserJet MFP M232-M237"
    serial: str = ""                 # 序列号
    vendor: str = "HP"               # 厂商
    has_adf: bool = False            # 是否有输稿器
    has_duplex: bool = False         # 是否支持双面
    has_platen: bool = True          # 是否有平板
    max_width: int = 2550            # 最大扫描宽度（1/300英寸）
    max_height: int = 4200           # 最大扫描高度（1/300英寸）
    resolutions: list = [75,150,200,300,600]    # 支持的分辨率列表
    color_modes: list = ["RGB24","Grayscale8"]  # 支持的颜色模式
    formats: list = ["image/jpeg","application/pdf"]  # 支持的输出格式

    # GUI 扩展字段（运行时添加，不参与序列化）
    custom_name: str = ""            # 用户自定义名称，存在于 nicknames 配置中

    @property
    def display_name(self) -> str:
        return self.model or self.name
```

### 5.2 ScanJob（保留，当前未用于轮询）

```python
@dataclass
class ScanJob:
    job_id: str
    job_uri: str
    state: str = "Pending"
```

### 5.3 格式映射表

```python
FORMAT_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
    "pdf": "application/pdf",
}

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "application/pdf": "pdf",
    "application/octet-stream": "jpg",  # 未知 MIME 降级为 jpg
}
```

---

## 6. 配置文件规范

### 6.1 文件位置

- **开发模式**：`HP_Scan_Tool/scan_config.json`（脚本同级目录）
- **EXE 模式**：`HP_Scan_Tool.exe 所在目录/scan_config.json`

### 6.2 JSON Schema

```json
{
  "theme": "dark",
  "resolution": 300,
  "color_mode_ui": "彩色",
  "output_format": "jpg",
  "output_dir": "D:\\Scans",
  "saved_ips": [
    {"ip": "x.x.x.x", "model": "HP LaserJet MFP M232-M237"},
    {"ip": "x.x.x.x", "model": "HP LaserJet MFP M232-M237"}
  ],
  "nicknames": {
    "x.x.x.x": "财务室打印机",
    "x.x.x.x": "三楼 HP 7720"
  }
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `theme` | string | `"dark"` | 界面主题，`"dark"` 或 `"light"` |
| `resolution` | int | `300` | 上次使用的扫描分辨率 |
| `color_mode_ui` | string | `"彩色"` | 上次使用的颜色模式（UI 显示值） |
| `output_format` | string | `"jpg"` | 上次使用的输出格式 |
| `output_dir` | string | `~/Documents/HP_Scans` | 扫描文件保存目录 |
| `saved_ips` | array | `[]` | 已发现的打印机 IP 和型号 |
| `nicknames` | object | `{}` | 用户自定义名称，key=IP, value=名称 |

---

## 7. UI 规范

### 7.1 布局结构

```
┌─────────────────────────────────────────────────────┐
│ 惠普集成扫描工具                     [light│dark]    │ ← 顶栏
├────────────────────┬────────────────────────────────┤
│ 扫描仪             │ 就绪 — 已加载 3 台历史打印机     │ ← 状态条
│ [搜索局域网][手动添加]│                                │
│                    │ 分辨率    颜色    格式    来源    │
│ ┌────────────────┐ │ [300▼] [彩色▼] [jpg▼] [平板▼]  │
│ │232dwc-2        │ │                                │
│ │IP x.x.x.x│ │ 保存到 [__________] [浏览]      │
│ │[eSCL][ADF]     │ │                                │
│ └────────────────┘ │ [查询能力][预览状态]    [扫描]   │
│ ┌────────────────┐ │                                │
│ │232dwc-1        │ │                                │
│ │...             │ │                                │
│ └────────────────┘ │                                │
├────────────────────┴────────────────────────────────┤
│ ████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │ ← 进度条（仅扫描中显示）
└─────────────────────────────────────────────────────┘
```

### 7.2 卡片状态

| 状态 | 边框颜色(light/dark) | 边框宽度 |
|------|---------------------|---------|
| 未选中 | `gray55` / `gray40` | 1px |
| 选中 | `#3B8ED0` / `#1F6AA5` | 2px |

### 7.3 对话框

| 对话框 | 类型 | 触发 | 内容 |
|--------|------|------|------|
| 能力查询结果 | showinfo | 点击"查询能力" | 型号/序列号/最大DPI/ADF/双面/平板/颜色模式/格式 |
| 打印机状态 | showinfo | 点击"预览状态" | State(Idle/Processing/Unknown) + ADF有纸(是/否) |
| 扫描完成 | askyesno | 扫描成功 | "已保存: {路径}" + "打开所在文件夹？" |
| 扫描失败 | showerror | 扫描异常 | 异常类型+消息+traceback + 排查提示 |
| 手动添加 | CTkToplevel | 点击"手动添加" | IP输入框 + 探测按钮 + 结果反馈 |
| 重命名 | CTkToplevel | 右键卡片 | 名称输入框 + 确认按钮 |

### 7.4 通知方式

| 优先级 | 方式 | 场景 |
|--------|------|------|
| 信息 | 状态栏文字更新 | 发现完成、选中切换、ADF 自动切换 |
| 警告 | messagebox.showwarning | 未选打印机就点扫描/查询 |
| 错误 | messagebox.showerror | 扫描失败 |
| 进度 | 底部不确定进度条 | 搜索中、扫描中 |

---

## 8. 错误处理矩阵

### 8.1 eSCL 引擎错误

| 异常类型 | 触发条件 | 处理方式 | UI 表现 |
|---------|---------|---------|--------|
| `RuntimeError: 未配置 eSCL URL` | escl_url 为空时调用 execute_scan | 抛出 | messagebox: "该打印机未探测到 eSCL，将尝试 WIA" |
| `RuntimeError: 创建扫描任务失败 HTTP {code}` | POST ScanJobs 返回非 2xx | 抛出 + 附响应体前 300 字符 | showerror: traceback |
| `RuntimeError: 无法获取 ScanJob URI` | 201 但无 Location 且响应体无 JobUri | 抛出 | showerror: traceback |
| `TimeoutError: 扫描超时（{t}s）` | NextDocument 轮询超时 | 抛出 | showerror: 含排查提示 |
| `requests.RequestException` | 网络超时/连接拒绝 | 轮询中静默忽略 | 继续下一轮 |
| `Exception` (fetch_capabilities) | XML 解析失败 | `pass` 吞掉 | 返回原 ScannerInfo 对象 |

### 8.2 GUI 错误

| 异常类型 | 处理方式 |
|---------|---------|
| 扫描线程内任何异常 | `traceback.format_exc()` → `_scan_done(False, tb)` |
| WIA 扫描失败 | 返回 None → showerror |
| 配置文件读写异常 | `try/except pass`，不影响程序运行 |
| 目录创建异常 | showerror: "无法创建目录" |

### 8.3 用户排查提示

扫描失败弹窗底部附排查指引：
```
如果 eSCL 不通，请确认:
1. 打印机已启用 eSCL/AirScan
2. 浏览器访问 http://打印机IP/eSCL/ScannerStatus 确认可达
3. 防火墙未拦截
```

---

## 9. 打印机兼容性矩阵

### 9.1 已测试机型

| 机型 | IP | eSCL Server | ADF | 双面 | eSCL 扫描 | WIA 扫描 | 备注 |
|------|----|-----------|-----|------|----------|---------|------|
| HP OfficeJet Pro 7720 | x.x.x.x | nginx/2.62 | 有 | 支持 | ✅ | — | 完整 eSCL 支持，支持 job URI 轮询 |
| HP LaserJet MFP M232-M237 | x.x.x.x | ApolloLedmServer/2.63 | 无 | 不支持 | ✅ | — | eSCL 扫描需 NextDocument 直接轮询 |
| HP LaserJet MFP M232-M237 | x.x.x.x | ApolloLedmServer/2.63 | 无 | 不支持 | ✅ | — | 同上，ScannerCapabilities 返回 301→HTTPS |
| HP LaserJet MFP M232-M237 | x.x.x.x | ApolloLedmServer/2.63 | 有 | 不支持 | ✅ | — | 同上 + ADF 输稿器 |
| HP LaserJet M1216nfh | x.x.x.x | Marvell Mrvl-R1_0 | — | — | ❌ | ❌ | 不支持任何网络扫描协议，仅 JetDirect 打印 |

### 9.2 兼容性判断决策树

```
打印机接入局域网
    │
    ├── GET /eSCL/ScannerStatus → 200
    │   └── eSCL 可用 ✅
    │       ├── POST ScanJobs → 201 → 可用
    │       └── 404/503 → 不可用
    │
    ├── 端口 5357 开放 (WSD) → WSD 可用（未实现）
    │
    ├── Windows WIA 能找到 → WIA 降级可用 ✅
    │
    └── 以上都不满足 → 不支持 ❌
```

---

## 10. 构建与分发

### 10.1 构建命令

```bash
cd <PROJECT_DIR>
python build_exe.py
```

产物：`dist\HP_Scan_Tool.exe`（~22 MB）

### 10.2 开发运行

```bash
cd <PROJECT_DIR>
pip install -r requirements.txt
python hp_scan_gui.py
```

或双击 `run.bat`

### 10.3 分发方式

单文件便携 EXE，无需安装、无需管理员权限。拷贝到任意目录即可运行。配置文件在 EXE 同级目录自动生成。

### 10.4 首次使用流程

```
双击 HP_Scan_Tool.exe
    ↓
自动 mDNS 搜索 4 秒 → 列出局域网打印机
    ↓
首次无历史 → 显示 "发现 N 台打印机 (+N 新增)"
    ↓
单击选中 → 调整参数 → 点"扫描"
    ↓
扫描完成 → 弹窗 "打开文件夹？"
```

---

## 11. 待修复项 (To Fix)

> **重要**：以下问题需要在后续版本中修复，其他 Agent 接手本项目时应优先处理这些项。

| # | 优先级 | 问题 | 根因 | 修复方向 | 状态 |
|---|--------|------|------|---------|------|
| 1 | 🔴 P0 | ~~EXE 关闭后进程残留~~ | ~~daemon 线程未强制退出~~ | ~~destroy 重写 + os._exit(0)~~ | ✅ 已修复 (Bug #9) |

---

## 12. Bug 修复历史

| # | 日期 | 问题 | 根因 | 修复 |
|---|------|------|------|------|
| 1 | 07-15 | ADF 列始终显示"?" | 三元表达式写死 + 自动发现未调 fetch_capabilities | 三处增加 fetch_capabilities 调用，三元改为三态 |
| 2 | 07-15 | UI 对比度不足、文字截断 | 标签浅灰底白字不可读 | 全部标签改用高对比度深色底(#1b7c6e等)+白字 |
| 3 | 07-16 | 每次启动都搜索 | ①配置存%TEMP%被清 ②探活阻塞UI | ①EXE模式用sys.executable目录 ②后台异步probe_saved |
| 4 | 07-16 | 保存路径重启重置 | _browse没调save_config | 选择目录后立即save_config |
| 5 | 07-16 | 扫描超时报None | M232 job URI永远404 | 改为直接轮询NextDocument |
| 6 | 07-17 | 清理未使用导入 | 代码审查 | 移除escl_engine中os/sys/uuid/datetime |
| 7 | 07-17 | 超时错误写死90s | 代码审查 | 改为动态参数 |
| 8 | 07-17 | build_exe缺customtkinter | 代码审查 | 补hidden-import |
| 9 | 07-16 | **EXE关闭后进程残留** | mainloop结束后未强制退出，Zeroconf未close，后台线程继续运行 | destroy()重写+os._exit(0)兜底，main()后加os._exit(0)，_scan_cancel事件通知后台线程 |
| 10 | 07-16 | **UI参数变更不同步** | 分辨率/颜色/格式下拉框无command回调，扫描时仍用配置加载的旧值 | 三个下拉框添加lambda回调实时写入实例变量 |
| 11 | 07-16 | **M232能力查询失败** | ScannerCapabilities返回301→HTTPS，自签名证书SSL验证失败 | escl_engine全部HTTP请求加verify=False，urllib3禁用InsecureRequestWarning |
| 12 | 07-16 | **扫描无取消机制** | 关闭窗口时扫描线程继续运行，无法优雅中断 | 添加threading.Event(_scan_cancel)，destroy时set，扫描前检查 |
| 13 | 07-16 | **来源选择不持久化** | source未存入scan_config.json，重启后总是回到"平板" | 配置新增source字段，select_scanner/caps_done尊重已保存值 |
| 14 | 07-16 | **探活后丢失选中高亮** | _rebuild_cards重建卡片后未恢复选中状态 | _rebuild_cards末尾检查selected_idx并调用set_selected(True) |
| 15 | 07-16 | **手动添加不支持自定义端口** | probe_escl调用硬编码port=80 | 对话框新增端口输入框，传递给probe_escl |
| 16 | 07-16 | **run.bat版本号过旧** | 仍显示v2 | 更新为v3.1，适配机型列表更新 |
| 17 | 07-16 | **手动添加探测冻结UI** | probe+fetch_capabilities在主线程同步执行 | 移入后台线程+winfo_exists检查 |
| 18 | 07-16 | **对话框关闭后probe崩溃** | 同步probe期间关闭对话框操作已销毁widget | 后台线程+widget存在性检查 |
| 19 | 07-16 | **分辨率回调无防护** | int(v)对非数字输入崩溃 | v.isdigit()防护 |
| 20 | 07-16 | **配置损坏启动崩溃** | saved_ips条目缺ip键时KeyError | isinstance+get()校验跳过无效条目 |
| 21 | 07-16 | **WIA循环逻辑错误** | for循环首次迭代就return | 改continue模式遍历所有item |
| 22 | 07-16 | **scanners跨线程半更新** | 后台就地修改scanner属性 | copy.copy新对象后原子替换 |
| 23 | 07-16 | **_query_caps跨线程引用** | 后台读self.selected可能已切换 | 启动线程前捕获局部引用 |
| 24 | 07-16 | **_check_status跨线程引用** | 后台读self.selected.escl_url | 拷贝url到局部变量 |
| 25 | 07-16 | **probe_escl冗余请求** | 同端点有无斜杠两次请求 | 规范化路径只发一次 |
| 26 | 07-16 | **无HTTP Session复用** | 轮询每次新建TCP连接 | requests.Session复用 |
| 27 | 07-16 | **未使用ScanJob dataclass** | 定义未实例化 | 移除 |
| 28 | 07-16 | **未使用命名空间常量** | ns_escl等未使用 | 移除 |
| 29 | 07-16 | **SettingProfiles空操作块** | if块只有pass | 移除 |
| 30 | 07-16 | **WIA变量名ip遮蔽** | ip指ImageProcess非IP地址 | 重命名为img_proc |
| 31 | 07-16 | **宽泛except静默吞异常** | 多处except:pass | 窄化+logging.debug |
| 32 | 07-16 | **custom_name未声明** | 动态赋值不在dataclass | ScannerInfo新增字段 |
| 33 | 07-16 | **hasattr冗余检查** | custom_name已是dataclass字段 | 全部移除 |
| 34 | 07-16 | **刷新无条件重置选中** | 手动刷新丢失选中 | 仅新设备加入时重置 |
| 35 | 07-16 | **dir_entry无效路径** | 手动输入无效路径到扫描才报错 | 提前验证 |
| 36 | 07-16 | **无扫描进度反馈** | 只有indeterminate进度条 | 状态栏显示阶段文字 |
| 37 | 07-16 | **配置非原子写入** | 写入中断损坏配置 | .tmp+os.replace |
| 38 | 07-16 | **WIA try_wia_scan静默失败** | except:return None无日志 | logging.debug |
| 39 | 07-16 | **自动曝光功能** | 扫描件灰底、对比度低 | PC端后处理：直方图拉伸(1%-99%分位)，UI复选框+配置持久化 |

---

## 13. 已知限制

| # | 限制 | 影响 | 优先级 | 备注 |
|---|------|------|--------|------|
| 1 | 单线程扫描，不支持并发 | 一次只能扫描一台 | P2 | 当前用户场景无需并发 |
| 2 | 不支持 WSD 协议 | WIA 兜底需要 Windows 识别到设备 | P3 | WSD 实现复杂度高 |
| 3 | WIA 仅 USB | 网络扫描仪 WIA 不可用 | P2 | Windows WIA 栈限制 |
| 4 | ~~M232 ScannerCapabilities 走 HTTPS 301~~ | ~~自签名证书导致能力查询失败~~ | ~~P2~~ | ✅ 已修复 (Bug #11)：全部请求加 verify=False |
| 5 | 不支持扫描预览 | 扫描前无法预览 | P3 | 需要 eSCL Preview 端点 |
| 6 | 无扫描历史记录 | 扫描完成后无日志 | P3 | 可在后续版本加入 |
| 7 | 不支持多页 PDF | ADF 扫描仍导出单页 | P2 | M232 不支持原生 PDF |

---

## 14. 测试场景

### 13.1 功能测试

| 场景 | 步骤 | 预期结果 |
|------|------|---------|
| 首次启动 | 无 scan_config.json 时启动 | mDNS 自动搜索，列出局域网打印机 |
| 二次启动 | 已有历史记录时启动 | 秒加载历史打印机，不显示搜索动画 |
| 平板扫描 | 选中 M232 → 默认"平板" → 点"扫描" | 8-12 秒完成，生成 JPG |
| ADF 扫描 | 选中 232dwc-2 → 放入纸张 → 扫描 | 自动切换 Feeder，逐张吸入 |
| ADF 空载 | 选中 232dwc-2 → 不放纸 → 扫描 | 自动切换 Platen，平板扫描 |
| 手动添加 | 输入 x.x.x.x → 探测 | eSCL 探通，追加到列表 |
| 手动添加无效IP | 输入 1.2.3.4 → 探测 | 提示"不支持 eSCL" |
| 重命名 | 右键 232dwc-2 → 输入"财务室" → 确认 | 卡片名称变为"财务室"，重启保留 |
| 主题切换 | light→dark→light | 全局控件联动，无低对比度 |
| 目录选择 | 浏览 → 选 D:\test → 退出重启 | 保存目录仍为 D:\test |
| 分辨率切换 | 选 600DPI → 扫描 | 生成高分辨率图片 |
| PDF 格式 | 选 PDF → M232 扫描 | 自动 JPEG→PDF 转码 |
| 扫描失败 | 断开网线 → 扫描 | 弹窗显示 TimeoutError + 排查指引 |

### 13.2 边界测试

| 场景 | 预期 |
|------|------|
| 无网络 | 启动不崩溃，列表为空，状态栏"未发现打印机" |
| 配置文件损坏 | 静默降级为默认配置 |
| EXE 移动位置 | 配置丢失（合理行为，重新搜索即可） |
| 长打印机名称 | wraplength=260 自动换行 |

---

## 15. 未来规划

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P1 | 扫描历史记录 | 列表展示最近扫描，支持一键打开 |
| P1 | 多页 PDF 合成 | ADF 多页扫描 → 合并为单 PDF |
| P2 | 扫描预览 | 低分辨率预览 → 确认 → 正式扫描 |
| P2 | WSD 协议支持 | 扩展兼容老旧打印机 |
| P2 | 扫描区域裁剪 | 手动指定扫描区域 |
| P3 | 批量扫描 | 多台打印机并发 |
| P3 | 命令行模式 | 支持 `HP_Scan_Tool.exe --scan --ip=x --output=file.jpg` |
| P3 | macOS 支持 | 跨平台移植（需替换 WIA 为 ICA） |
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
