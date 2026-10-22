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

> ⚠️ **2026-10-20 起，曝光/预设已并回本项目**：放弃独立 DocFix 软件，基础曝光/滤镜在本项目内闭环。依据 `docs/adr/0008-exposure-enhancement.md` + `docs/design/2026-10-20_新功能调研与方案_双面_续扫_纠偏_曝光_v1.0.md` §0 需求 F。现状：`exposure.py` / `preset_manager.py` 已入库，`ExposurePanel` 已接入两个预览对话框，曝光已接进落盘链路（`save_manager.py`），预设持久化于 `%APPDATA%\HP_Scan_Tool\presets.json`。
> （历史：v3.3—2026-10-20 期间曝光/预设曾剥离至独立工具 DocFix，该表述于 2026-10-20 作废。）

> 版本: v3.1 | 日期: 2026-07-16 | 作者: Marvis

> ⚠️ **2026-10-21 文档一致性订正（本次）**：本文档正文写于 v3.1（2026-07-16，CustomTkinter 时代），仓库现为 **v4.0（PySide6）**。
> 本次只做**文字订正**，不改章节结构；已按代码现状就地订正的是**可逐条核实的点**（函数/模块名、文件路径、依赖项、体积与行数等数字）。
> 仍然描述 v3.1 实现、但本轮**未改写**的部分（§2.6 UI 规格、§7 UI 规范、§8 错误处理矩阵等）统一在正文里就地标注了 `⚠️ v3.1 遗留`，**以代码为准**。

> ⚠️ **2026-10-21 批次 4 功能规格补充（本次）**：§2.4 新增 §2.4.6 ~ §2.4.9 四节，对应已落地并可逐条核实的四项能力 —— **手动双面扫描 / 断点续扫 / 大批量合并成单个 PDF / 自动纠偏 + 倾斜强提醒**。
> 依据代码现状写成（`document_session.py` / `doc_merge.py` / `save_manager.save_session` / `hp_scan_gui_pyside.SkewWarningDialog` / `skew_detect.py`），**不含任何尚未实现的能力**。

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
| 现代 UI | HP Smart 臃肿、Web 界面卡顿 | PySide6 本地原生窗口（v4.0 已从 CustomTkinter 迁移） |
| 单文件便携 | 需要安装步骤和管理员权限 | 单文件 EXE **62.95 MiB**（2026-10-22 23:4x 用最终代码重打实测 = **66,016,337 字节**；旧数字 66,003,273 / 66,004,536 / 65,987,523 / 65,866,395 字节是此前几次的包、64,968,184 字节 / 约 62 MB 是 **2026-10-20 改造前基线**，均已过期），U 盘即走 |

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
│             hp_scan_gui_pyside.py            │
│         (PySide6 UI 主程序)                  │
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

> ⚠️ **v3.1 遗留**：本图是 v3.1 的模块视角。v4.0 实际新增了 `scan_coordinator`（扫描编排）/`cache_manager`（磁盘缓存）/`save_manager`（落盘）等中间层，GUI 不再直连引擎 —— 见 §3.2 当前文件结构。

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
| 超时 | 2 秒（`escl_engine.probe_escl` 默认 `timeout=2.0`；发现流程 `scan_coordinator.py:509` 传 3 秒） |

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
| 加载时机 | ⚠️ **v3.1 遗留**：v3.1 写的是 `_restore_presets()` — `__init__` 末尾同步调用；v4.0 的 PySide6 GUI **已无该方法**（`saved_ips` 现在只用于"手动添加"时补写，见 `hp_scan_gui_pyside.py:2269-2272`） |
| 探活策略 | ⚠️ **v3.1 遗留**：v3.1 写的是 `_probe_saved()` — 后台逐台调用 probe_escl，每台探通后 `after(0, _rebuild_cards)` 异步刷新 UI；v4.0 **已无该方法**，发现统一走 `ScanApp._auto_discover()` → `coordinator.discover_scanners_background(...)` → `_on_discover_finished()` → `_rebuild_cards()` |
| 去重 | 按 `ip` 字段去重 |

### 2.3 能力查询模块

#### 2.3.1 ScannerCapabilities 解析

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 实现函数 | `escl_engine.py::fetch_capabilities(scanner, timeout)` |
| 端点 | `GET {escl_url}/ScannerCapabilities` |
| 超时 | 3 秒（`escl_engine.fetch_capabilities` 默认 `timeout=3.0`） |
| 命名空间 | 支持 `imaging/escl/2011/05/03` 和 `scanner/escl/2011/09` 两种 |

**解析字段对照表**：

| XML 元素 | ScannerInfo 字段 | 默认值 |
|----------|-----------------|--------|
| `Model` | `model` | 空字符串 |
| `SerialNumber` | `serial` | 空字符串 |
| 顶层 `Platen` 子元素存在 | `has_platen = True` | True |
| 顶层 `Adf` 子元素存在 | `has_adf = True` | False |
| `Adf/Duplex` = "true" | `has_duplex = True` | False |
| `MaxWidth` | `max_width` | 2480（dataclass 默认，`escl_engine.py:132`）；解析时该标签缺省取 2550 |
| `MaxHeight` | `max_height` | 3508（dataclass 默认，`escl_engine.py:133`）；解析时该标签缺省取 4200 |
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
| 用途 | ① 打印机状态对话框 ② 扫描前 ADF 自动检测 |

### 2.4 扫描执行模块

#### 2.4.1 eSCL 扫描流程

| 属性 | 值 |
|------|---|
| 优先级 | P0 |
| 实现函数 | `escl_engine.py::execute_scan()`（返回 `(图片字节, 扩展名)`；**不落盘**） |
| 超时 | 30 秒（`escl_engine.execute_scan` 默认 `timeout=30.0`）；协调器 `scan_coordinator.py:838` 传 90 秒。⚠️ 多页路径 `execute_multipage_scan` 默认 90 秒，协调器传 300 秒 |
| 轮询间隔 | 0.5 秒 |

> **2026-10-21 订正**：本节原写「`execute_scan()` → `scan_to_file()`」。`scan_to_file()` 已于 `50aeb59` 删除（死代码、零调用），`execute_scan()` 之后**不再有引擎层的落盘函数**——落盘已下沉到 `save_manager`（见 §2.4.3）。

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
| 来源 (source) | Platen（平板）, Feeder（输稿器）, 自动 | GUI 下拉是**中文**三项：「平板」/「ADF」/「自动」（`hp_scan_gui_pyside.py:1776-1779`）；「自动」由 `scan_coordinator.resolve_source` 按 ADF 有纸状态判定 |
| 双面 (duplex) | true/false | 仅 ADF 支持时可用 |

> ⚠️ **2026-10-21 补充**：本表的「双面 (duplex)」指**扫描仪原生双面**（`escl_engine` 在 ScanJob XML 里下发 `<scan:Duplex>`，需来源是输稿器且机型支持）—— 与 §2.4.6 的「双面（手动）」（两趟单面 + 本地合并页序）**是两件不同的事**：手动双面模式**下发给引擎的是 `duplex=false`**。

#### 2.4.3 PDF 合成

**打印机永远只出图片，PDF 由本地合成**（`escl_engine.py:716` 的口径）：选 PDF 时请求层仍要 `image/jpeg`（`REQUEST_FORMAT["pdf"] = ("image/jpeg", "pdf")`），落盘层再用 Pillow 把图片合成为 PDF：

```
execute_scan 返回 (jpeg_bytes, "jpg")        # 请求的是 jpg，落盘扩展名标为 pdf
    ↓
缓存落 page_001.jpg（扩展名撒谎由 save_manager 归一）
    ↓
save_manager._write_pdf()  ← 逐页 Image.open + 合成 A4 画布 + 保存 PDF
    ↓
返回 pdf_path
```

> **2026-10-21 订正**：本节原写「引擎用 Pillow 把 JPEG 转为 PDF」，流程图里的 `scan_to_file 检测 output_format="pdf" && ext!="pdf"` 随 `scan_to_file()` 一起于 `50aeb59` 删除。现在**引擎层零 PDF 转换**，转换点唯一且在 `save_manager._write_pdf()`（`save_manager.py:738`）。

#### 2.4.4 ADF 自动检测

扫描前自动检查输稿器状态，无需用户手动切换来源：

```
_start_scan（GUI）→ ScanCoordinator 调度
    ├── if scanner.has_adf:
    │     get_scanner_status() → adf_loaded?
    │     ├── True  → source="Feeder", 状态栏提示"输稿器检测到纸张"
    │     └── False → source="Platen", 状态栏提示"输稿器为空"
    └── execute_scan(source=source_val)
```

> **2026-10-21 订正**：流程图首行原写 `_start_scan → _do_scan`；`_do_scan` 在 v4.0 **不存在**（PySide6 GUI 的 `_start_scan` 直接交给 `ScanCoordinator`）。

#### 2.4.5 WIA 降级扫描

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 触发条件 | eSCL 不可用（escl_url 为空）时点击扫描 |
| 实现文件 | `wia_engine.py` |
| 依赖 | pywin32 (win32com) |
| 流程 | `list_wia_scanners()` → 取首个扫描仪 → `wia_scan()`（失败降级再 `try_wia_scan()`） |

> **2026-10-21 订正**：本行原写 `wia_scan_to_file()`。该函数**在当前代码里不存在**（`wia_engine.py` 的 `__all__` 只有 `list_wia_scanners` / `wia_scan` / `try_wia_scan`），属文档描述了一个不存在的函数。

#### 2.4.6 手动双面扫描（输稿器）

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 入口 | GUI 参数区「扫描模式」单选 → **「双面（手动）」** |
| 前提 | **必须用输稿器（ADF）** —— 平板没有背面；本功能与扫描仪自带的「双面扫描」复选框无关 |
| 实现方式 | **两趟单面**：正面一趟 → 用户把整叠纸**翻面**放回输稿器 → 背面一趟；下发给引擎的是 `"duplex": False`，合并在本地做 |
| 页账本 | `PageRef.side` ∈ `("single", "front", "back")`（`document_session.SIDES`） |
| 会话状态 | `scanning` →（正面扫完）`awaiting_flip` →（背面扫完）合并预览 → `completed` |
| 页序 | `doc_merge.interleave_duplex` —— **先反转、再交错、多余的接末尾**：背面整叠翻面 ⇒ 扫入顺序相反 ⇒ 先 `reversed(back)`，再 F1/B末、F2/B末-1… 交错；两边页数不等时多出的那几页按原序接在末尾 |
| 可配置 | 「背面顺序反转」复选框（默认勾选）⇒ 写入会话字段 `reverse_back`；背面顺序本来就正常时取消勾选（**仍然交错，不是拼接**） |
| 限制 | 只有输稿器可用；会话进行中改选「单面」⇒ 放弃暂存的正面（改选「续扫」不放弃） |

#### 2.4.7 断点续扫

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 入口 | ① 倾斜强提醒弹窗的「中断并从第 N 页重扫」（当前 GUI 里触发该动作的**唯一入口**，没有独立的"指定起始页"输入框）；② 会话状态 `paused` / `awaiting_flip` → `scanning`（`DocumentSession.resume_scanning`） |
| 行为 | 丢弃**该批次**里第 N 页及之后的页、保留前 N-1 页（对应缓存文件同时删除，页元数据交由既有孤儿清理） |
| 范围 | 丢弃范围**只限该 job 批次**，其它批次已扫好的页不受影响 |
| 前提 | **设备仍可用** + **已扫的页还在缓存里** |
| 落盘一致性 | 删除意图必须活过落盘 ⇒ `SessionStore.save()` 做 base/mine/theirs **三方逐页合并**，"我删过的页"删除优先 ⇒ 已删页不会被磁盘上的旧副本复活；并发追加的页两边都保留 |
| 跨进程恢复 | 会话文件在 `%APPDATA%\HP_Scan_Tool\sessions\`；下次启动弹「有未完成的文档会话，是否继续？」，并**重新登记**会话引用的 job 目录保护（保护集合是进程内的，重启后为空） |

> ⚠️ **前提的代价**：页的物理载体是**磁盘缓存** ⇒ 缓存过期（`cache_config.json` 的 `stale_hours`，默认 24 小时）后页会被清掉 ⇒ **隔天再续扫可能已经无页可续**。

> ⚠️ **生效范围**：只有**会话模式**（双面 / 续扫）才有"续扫"语义 —— 单面模式没有会话对象，此时点「中断并从第 N 页重扫」只是中止这次扫描，已扫的页留在缓存里、不进任何会话。

#### 2.4.8 大批量合并成单个 PDF

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 入口 | GUI「结束并保存」⇒ **合并预览**（页序清单 + 缩略图，可逐页查看）⇒「保存并结束」 |
| 实现 | `save_manager.save_session`（B4-5）；页序一律委托 `doc_merge`（`save_manager` 与 GUI 预览走同一套调用，不会两处漂移） |
| 产物 | 输出格式选 PDF ⇒ 合成**一个** PDF（复用两阶段流式合成 `_write_pdf`）；选 jpg/png/tiff ⇒ 每页一个文件，命名带**全局页号**（跨批次连续） |
| 页序 | 双面 = 交错（见 §2.4.6）；批次 / 续扫 = **批序 + 批内原序**（按 job_id 分组、首次出现顺序，组内原序，`doc_merge.merge_batches`） |
| 内存门禁 | 合成峰值 ≈ 2.1 × 产物 PDF 体积；预计峰值超过 **1.5GB** 门禁时先问「仍要继续吗」，**绝不静默截断**（少页比失败更糟） |
| 失败处理 | 一页都没存下来 ⇒ 明确报错，**不产出 0 字节文件**；部分页失败 ⇒ **保留缓存目录**以便重试，会话回到 `paused` |
| 清理 | 全部页成功后：解除 job 目录保护 → 删缓存目录 → 清页元数据；失败时**什么都不清** |

#### 2.4.9 自动纠偏与倾斜强提醒

| 属性 | 值 |
|------|---|
| 优先级 | P1 |
| 检测 | 每页写入缓存后调 `skew_detect.estimate_skew`（纯 PIL 投影法，搜索范围 ±8°，检出下限 0.75°）；空白页 / 墨迹过少返回 None ⇒ 不判角度、不纠偏、不提醒 |
| 记录 | 角度写入 `page_meta.deskew_angle`（**绝不旋转缓存原件**） |
| 提醒条件 | 连续 `skew_warn_consecutive` 页（默认 **3** 页）角度绝对值 **>** `skew_warn_threshold`（默认 **3.0°**）⇒ 协调器发 `skew_warning` 信号 ⇒ GUI 弹**模态强提醒**（`SkewWarningDialog`，带该页缩略图） |
| 强提醒的三个选项 | 「继续扫描（自动纠偏）」/「中断并从第 N 页重扫」/「强制保存（不纠偏）」 |
| 施加 | 落盘时按 `deskew_apply_threshold`（默认 **0.5°**）施加纠偏（`skew_detect.deskew_image`）；「强制保存」的页（`PageRef.force_keep`）**不纠偏、原样保存** |
| 阈值可改性 | ⚠️ **界面里没有纠偏开关** —— `deskew_enabled` / `deskew_apply_threshold` / `skew_warn_threshold` / `skew_warn_consecutive` 这 4 个键只有默认值（`save_manager.DESKEW_CONFIG_DEFAULTS`），界面不可调 |

> ⚠️ 这四项**只在 GUI 提供入口**：`cli.py` 的子命令仍是 scan / discover / status / history，**没有**双面 / 续扫 / 合并 / 纠偏相关的 CLI 参数。

### 2.5 配置持久化模块

#### 2.5.1 配置文件路径策略

```
# scan_coordinator.py:252-220 / config_io.py:config_dir()
def _app_dir():
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        appdata = os.path.expanduser("~")
    path = os.path.join(appdata, "HP_Scan_Tool")
    os.makedirs(path, exist_ok=True)
    return path

CONFIG_FILE = os.path.join(_app_dir(), "scan_config.json")
```

**为什么用 `%APPDATA%` 而不是 EXE 所在目录**：v3.1 曾把配置写在 EXE 同级目录（`sys.executable` 所在），v4.0 已统一改到 Windows 标准应用数据目录 `%APPDATA%\HP_Scan_Tool\`（与 `presets.json` / `history.json` / `profile` 同目录，避免 U 盘只读时写不进去）。

> **2026-10-21 订正**：本节原写 `_app_dir()` 优先取 `os.path.dirname(sys.executable)`、并解释"为什么不用 `__file__`"。当前实现是 `scan_coordinator.py:252` 的 `%APPDATA%` 版本，**EXE 同级目录不再写配置**。

#### 2.5.2 配置写入时机

| 操作 | 写入内容 | 触发函数 |
|------|---------|---------|
| 浏览选择目录 | `output_dir` | `_browse` → `save_config` |
| 开始扫描 | `resolution`, `color_mode_ui`, `output_format`, `output_dir` | `_start_scan` → `save_config` |
| ⚠️ 主题切换 | ~~`theme`~~ | ~~`_toggle_theme` → `save_config`~~ —— **v3.1 遗留**：全仓 `*.py` 搜 `theme` **零命中**，v4.0 代码里既无主题切换入口也无 `theme` 配置键 |
| 发现/刷新完成 | `saved_ips`, `nicknames` | ⚠️ **v3.1 遗留**：原写 `_do_discover` → `save_config`；`_do_discover` 在 v4.0 不存在（发现走 `_auto_discover()` → `ScanCoordinator`） |
| 重命名打印机 | `nicknames` | ⚠️ **v3.1 遗留**：原写 `set_custom_name` → `save_config`；v4.0 是 `_on_rename_requested()` → `_show_rename_dialog()` |

### 2.6 UI 交互模块

> ⚠️ **v3.1 遗留**：本节整体描述的是 **v3.1 的 CustomTkinter UI**（控件类名、尺寸、配色、交互均为 `ctk` 口径）。v4.0 已迁移到 **PySide6**（`hp_scan_gui_pyside.py`，主窗口 `ScanApp(QMainWindow)`、卡片 `ScannerCard(QFrame)`、对话框 `QDialog`），**窗口标题 `HP Scan Tool v4.0`、最小尺寸 900×620**。下列条目凡点名 `ctk.*` / `CTk*` 的，一律以代码为准。

#### 2.6.1 窗口规格

| 属性 | 值 |
|------|---|
| 默认尺寸 | ⚠️ v3.1 写 840×620 px（v4.0 实际：`setMinimumSize(900, 620)`，宽度随 `resize(width, height)` 自适应） |
| 最小尺寸 | ⚠️ v3.1 写 720×500 px（v4.0 实际：`hp_scan_gui_pyside.py:1086` → 900×620） |
| 标题 | "惠普集成扫描工具"（v4.0 实际：`HP Scan Tool v4.0`） |
| 布局 | 顶栏 + 左栏(扫描仪列表) + 右栏(操作面板) + 底部进度条 |

#### 2.6.2 主题系统

> ⚠️ **v3.1 遗留（整节失效）**：全仓 `*.py` 搜 `theme` **零命中**，`ctk.set_appearance_mode()` 在 v4.0 不存在，配置里也没有 `theme` 键。v4.0 是**单主题**（`resources/styles/app.qss` + `hp_scan_gui_pyside.py:152 COLORS`）。

| 属性 | 值 |
|------|---|
| 默认主题 | ⚠️ v3.1：上次使用的主题（配置持久化）—— **v4.0 无主题切换** |
| 切换控件 | ⚠️ v3.1：顶栏右侧 SegmentedButton [light / dark] —— **v4.0 无此控件** |
| 联动范围 | ⚠️ v3.1：全局 `ctk.set_appearance_mode()`，所有 CTk 控件自动适配 —— **v4.0 无此调用** |
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
| 刷新 | normal, "刷新" | — |
| 扫描 | normal, "扫描" | disabled, "扫描中..." |
| 打印机状态 | 始终可用 | — |

> **2026-10-21 订正**：本表原写「搜索局域网 / 查询能力 / 预览状态」三个按钮 —— 代码里**没有**这三个按钮：
> - 「搜索局域网」不存在，实际是「**刷新**」（`hp_scan_gui_pyside.py:1245`），点击触发后台重新发现；
> - 「查询能力」不存在（见 §7.3 订正）—— 能力是**启动时后台并行探测**（`hp_scan_gui_pyside.py:1156-1167` → 200ms 轮询 → `_rebuild_cards`），结果直接体现在**卡片标签**上，没有独立对话框；
> - 「预览状态」实际叫「**打印机状态**」（`hp_scan_gui_pyside.py:1391`）。

---

## 3. 技术架构

### 3.1 技术栈明细

| 层级 | 技术 | 版本要求 | 用途 |
|------|------|---------|------|
| 语言 | Python | ≥ 3.10 | — |
| GUI | PySide6 | ≥ 6.5 | 现代扁平 UI 框架（v4.0；v3.1 用的是 CustomTkinter 6.0.0，已弃用） |
| 网络发现 | python-zeroconf | ≥ 0.39 | mDNS/DNS-SD 服务发现 |
| WSD 发现 | wsdiscovery | ≥ 2.0 | WS-Discovery（UDP 3702 发现 / 5357 元数据） |
| HTTP 客户端 | requests | ≥ 2.28 | eSCL REST API 调用 |
| HTTP 底层 | urllib3 | ≥ 1.26 | requests 的传输层依赖 |
| XML 解析 | xml.etree.ElementTree | 内置 | eSCL XML 解析 |
| 图像处理 | Pillow (PIL) | ≥ 9.0 | 多页 PDF 合成 / A4 归一 / 曝光后处理 |
| PDF 读写 | pypdf | ≥ 6.0 | 多页 PDF 拆页与合并 |
| WIA 扫描 | pywin32 | ≥ 306 | Windows WIA COM 接口 |
| 打包 | PyInstaller | ≥ 6.0 | 单文件 EXE 打包 |

> **2026-10-21 订正**：`requirements.txt` 实有 **8** 条依赖，本表原为 7 条且 `GUI` 一行写的是已弃用的 CustomTkinter。已按 `requirements.txt` 逐条对齐（补 `wsdiscovery` / `urllib3` / `pypdf`，`pywin32` 下限对齐 `>=306`）。

### 3.2 文件结构

```
HP_Scan_Tool/
├── hp_scan_gui_pyside.py   # GUI 主程序（PySide6）
├── escl_engine.py          # eSCL/AirScan 协议引擎
├── wia_engine.py           # WIA 降级引擎（本地 USB 降级）
├── wsd_engine.py           # WS-Discovery 引擎（UDP 3702 发现 / 5357 元数据）
├── listen_engine.py        # 监听模式（等待面板触发）
├── scan_coordinator.py     # 扫描编排器（业务逻辑层）
├── save_manager.py         # 落盘（多页 PDF 合成 / A4 归一 / 曝光）
├── cache_manager.py        # 磁盘缓存管理
├── document_session.py     # 扫描会话模型（多页 / 分组 / 续扫）
├── doc_merge.py            # 双面 / 批次合并纯函数
├── exposure.py             # 曝光后处理（auto / manual）
├── skew_detect.py          # 倾斜角检测引擎（纯 PIL）
├── page_meta.py            # 每页元数据（页码 / 尺寸 / 来源）
├── preset_manager.py       # 曝光 / 扫描预设（内置 4 个 + 用户自定义）
├── profile_manager.py      # 按设备保存扫描参数
├── history_manager.py      # 扫描历史日志
├── config_io.py            # 配置 I/O 共享层（原子写 + 跨进程锁）
├── naming.py               # 跨入口命名形态（秒级时间戳 + 字母开头随机后缀；唯一性来自 uuid4）
├── cli.py                  # CLI 命令行入口
├── build_exe.py            # PyInstaller 打包脚本
├── tools/verify_scan.py    # 真机扫描验证脚本（产出 report.json）
├── requirements.txt        # pip 依赖声明
├── run.bat                 # 开发环境启动脚本
├── PRD.md                  # 本文件
├── tests/                  # pytest 套件（79 个 .py：conftest 1 + test_* 75 + 辅助模块 3）
├── docs/                   # 设计 / 审计 / 验证 / 计划文档
└── dist/
    └── HP_Scan_Tool.exe    # 最终交付物（2026-10-22 23:4x 用最终代码重打实测 66,016,337 字节 ≈ 62.95 MiB；旧数字 66,003,273 / 66,004,536 / 65,987,523 / 65,866,395 / 64,968,184 字节分别是此前几次的包与 2026-10-20 改造前基线）
```

> **2026-10-21 订正**：本节原是 v3.1 的 6 项文件清单（含 `hp_scan_gui.py` 这个**已改名**的文件、`scan_to_file()` 这个**已删除**的函数、`wia_scan_to_file()` 这个**从不存在**的函数），并带易过期的行数（752 / 452 / 169）。现按 `git ls-files` 的**当前根模块清单**重列，格式与 `README.md`「项目结构」一致（模块名 + 职责，不写行数）。行数在本仓会随并行开发漂移，不再写进文档。

### 3.3 线程模型

所有 IO 操作均在后台线程执行，UI 通过 `self.after(0, callback)` 回主线程更新：

| 操作 | 线程 | 阻塞时长 |
|------|------|---------|
| mDNS 发现 | 后台 daemon | ~4s |
| eSCL 探活 | 后台 daemon | ~3s/台 |
| 能力查询 | 后台 daemon | ~5s |
| 状态查询 | 后台 daemon | ~4s |
| 扫描执行（单页 / 多页） | 后台 daemon | 最多 **90s / 300s** |
| eSCL HTTP 调用 | 后台 daemon | **socket 超时 + 墙钟看门狗** 双保险（`escl_engine._http_call_bounded`） |
| UI 渲染 | 主线程 | — |

> ⚠️ **2026-10-21 订正**：原表写「扫描执行 最多 120s」—— 已过期。协调器实际传
> **单页 90 秒**（`scan_coordinator.py`）、**多页 300 秒**（`execute_multipage_scan`）。
> ★ **墙钟看门狗**是本轮新增的一层：实测 `requests` 的 socket 读超时在「对端接受连接
> 但迟迟不回数据」的状态下**可能长时间不触发**（抓到过卡 9 分钟的样本）⇒ 只靠
> socket 超时，一台半死不活的扫描仪就能把扫描线程永久卡死。墙钟到期按
> `ReadTimeout` 抛出，走既有的「设备无响应」中文提示链路。

### 3.4 打包配置

```python
# build_exe.py 核心参数（与 build_exe.py:97-131 实际命令一致）
"--onefile"              # 单文件 EXE
"--windowed"             # 无控制台窗口
"--name", "HP_Scan_Tool"
"--add-data", "resources;resources"   # 内嵌资源（字体/图标/QSS）
"--icon", "resources/icons/app.ico"    # EXE 图标
"--version-file", ...    # 现场生成 version_info.txt（v4.0.0.0）
# 项目内模块
"--hidden-import", "escl_engine" / "wia_engine" / "wsd_engine" / "listen_engine"
"--hidden-import", "scan_coordinator" / "save_manager" / "cache_manager"
"--hidden-import", "history_manager" / "profile_manager"
# 第三方依赖
"--hidden-import", "requests" / "urllib3" / "zeroconf"
"--hidden-import", "PIL" / "PIL.Image" / "xml.etree.ElementTree"
# 排除：Pillow 的 TYPE_CHECKING 分支会顺带拖进 numpy（含 OpenBLAS 约 9 MB）
"--exclude-module", "numpy"
"--clean"
"--noconfirm"
# 入口脚本
"hp_scan_gui_pyside.py"
```

> **2026-10-21 订正**：本节原写的 `--add-data escl_engine.py;.` / `--add-data wia_engine.py;.`（v3.1 靠 add-data 内嵌引擎，v4.0 改用 `--hidden-import`）与 `--hidden-import customtkinter`（已弃用依赖）均已不存在于 `build_exe.py`，"~22 MB" 也与实测不符。现按 `build_exe.py` 实际命令逐行对齐。

产物大小：**62.95 MiB**（2026-10-22 23:4x 用最终代码重打实测 = **66,016,337 字节**；含 Python 运行时 + 全部依赖 + 资源。旧文档写的 66,003,273 / 66,004,536 / 65,987,523 / 65,866,395 字节是此前几次的包、64,968,184 字节 / 约 62 MB 是 **2026-10-20 改造前基线**）

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

不同机型使用不同 XML 命名空间，引擎通过 `_strip_ns(el)` 函数剥离命名空间前缀实现兼容：

```python
def _strip_ns(el) -> str:
    return el.tag.split("}")[-1] if "}" in el.tag else el.tag
```

> **2026-10-21 订正**：原写 `tag(el)`，全仓 `*.py` 搜 `def tag` **零命中**；实际函数名是 `escl_engine._strip_ns(el)`（`escl_engine.py:35`）。

| 机型 | Server | 命名空间 |
|------|--------|---------|
| HP 7720 | nginx v2.62 | `imaging/escl/2011/05/03` |
| HP M232 | ApolloLedmServer v2.63 | `imaging/escl/2011/05/03` |
| HP M1216 | Marvell Mrvl-R1_0 | 不支持 eSCL |

### 4.4 POST ScanJobs XML 模板

```xml
<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings
    xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.0</pwg:Version>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <pwg:InputSource>Platen</pwg:InputSource>
  <scan:Intent>Document</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:Width>2550</pwg:Width>
      <pwg:Height>4200</pwg:Height>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <scan:XResolution>300</scan:XResolution>
  <scan:YResolution>300</scan:YResolution>
  <scan:ColorMode>RGB24</scan:ColorMode>
  <scan:Duplex>false</scan:Duplex>
  <!-- 仅 Feeder 来源时附加 -->
  <scan:MaxScanPages>1</scan:MaxScanPages>
</scan:ScanSettings>
```

> **2026-10-21 订正**：原模板与实际请求体不符（`escl_engine._build_scan_job_xml`，`escl_engine.py:659`）。逐项差异：
> - 根元素是 `<scan:ScanSettings>`，**不是** `<scan:ScanJob>`；
> - 命名空间走常量 `NS_SCAN` = `http://schemas.hp.com/imaging/escl/2011/05/03`（**不是** `.../scanner/escl/2011/09`）与 `NS_PWG`；
> - 多了 `<pwg:Version>2.0</pwg:Version>`；
> - 来源是 `<pwg:InputSource>`（**不是** `scan:InputSource`）；
> - 区域是 `<pwg:ScanRegions>/<pwg:ScanRegion>`（**不是** `scan:` 前缀）；
> - 偏移是 `<pwg:XOffset>` / `<pwg:YOffset>`（**不是** `scan:ScanRegionXOffset/YOffset`）；
> - Feeder 来源时追加 `<scan:MaxScanPages>`（原模板完全没有）。

**ContentRegionUnits 说明**：`escl:ThreeHundredthsOfInches`（1/300 英寸）是 eSCL 标准单位，因此 2550 = 8.5 英寸（Letter 宽度），4200 = 14 英寸（Legal 长度）。
> ⚠️ 注意区分：`_build_scan_job_xml` 的 `width`/`height` **函数默认**仍是 2550 / 4200（Letter/Legal）；而 `ScannerInfo.max_width/max_height` 的**dataclass 默认**是 2480 / 3508（A4 @300dpi）。两者用途不同，不要混用。

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
    max_width: int = 2480            # 最大扫描宽度（1/300英寸；A4 210mm @300dpi）
    max_height: int = 3508           # 最大扫描高度（1/300英寸；A4 297mm @300dpi）
    resolutions: list = [75,150,200,300,600]    # 支持的分辨率列表
    color_modes: list = ["RGB24","Grayscale8"]  # 支持的颜色模式
    formats: list = ["image/jpeg","application/pdf"]  # 支持的输出格式
    capabilities_known: bool = False  # 能力文档是否真的拿到（★ 设备到底有没有 ADF 的唯一权威信号）

    # GUI 扩展字段（运行时添加，不参与序列化）
    custom_name: str = ""            # 用户自定义名称，存在于 nicknames 配置中

    @property
    def display_name(self) -> str:
        return self.model or self.name
```

> **2026-10-21 订正**：原清单里 `max_width: int = 2550` / `max_height: int = 4200`，与 `escl_engine.py:132-133` 的 dataclass 默认**不符** —— 代码默认是 **2480 / 3508（A4 @300dpi）**，文档写的 2550/4200 是 Letter/Legal。另补上原先漏掉的字段 **`capabilities_known: bool = False`**（`escl_engine.py:145`）：它是「设备到底有没有 ADF」的**唯一权威信号** —— `fetch_capabilities` 真正拿到能力文档才置位（`escl_engine.py:434`），回退探测路径不会置位（`escl_engine.py:317`）。

### 5.2 ScanJob（⚠️ 当前代码里没有这个类）

```python
@dataclass
class ScanJob:
    job_id: str
    job_uri: str
    state: str = "Pending"
```

> **2026-10-21 订正**：本节原标题写「ScanJob（保留，当前未用于轮询）」，但**当前 `escl_engine.py` 里根本没有 `ScanJob` 这个类**（`git grep -n 'class ScanJob' -- '*.py'` 零命中；只有 `ScanJobCancelled` 异常类）。轮询用的 job URI 由 `_create_scan_job()` 以 `(base, job_uri, ext)` 元组返回，不经过数据结构体。原文保留在此仅作 v3.1 历史记录。

### 5.3 格式映射表

```python
# escl_engine.py:696-700
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
    "image/bmp": "bmp",
    "application/pdf": "pdf",
    "application/octet-stream": "jpg",  # 未知 MIME 降级为 jpg
}

# 用户选择的输出格式 → (向打印机请求的 MIME, 落盘扩展名)
REQUEST_FORMAT = {
    "jpg":  ("image/jpeg", "jpg"),
    "jpeg": ("image/jpeg", "jpg"),
    "png":  ("image/png",  "png"),
    "tiff": ("image/tiff", "tiff"),
    "tif":  ("image/tiff", "tiff"),
    "pdf":  ("image/jpeg", "pdf"),   # 请求图片，落盘时本地合成 PDF
}
```

> **2026-10-21 订正**：原文漏了 `MIME_EXT` 的 `"image/bmp": "bmp"` 一项，也**没有 `REQUEST_FORMAT`** —— 而 `REQUEST_FORMAT` 才是扫描请求真正用的表（`pdf` 请求的是 `image/jpeg`，PDF 由本地合成，见 §2.4.3）。`FORMAT_MIME` / `MIME_EXT` 已保留但不再用于请求层。

---

## 6. 配置文件规范

### 6.1 文件位置

- **统一位置**：`%APPDATA%\HP_Scan_Tool\scan_config.json`（开发模式与 EXE 模式**相同**）
- 同目录还有：`presets.json`（`preset_manager._presets_path()`）、`history.json`（`history_manager`）、各设备档案（`profile_manager`）

> **2026-10-21 订正**：本节原写「开发模式 = 脚本同级目录 / EXE 模式 = EXE 所在目录」。v4.0 已统一改到 Windows 标准应用数据目录 `%APPDATA%\HP_Scan_Tool\`（`scan_coordinator.py:252 _app_dir()` / `config_io.py:config_dir()`），**EXE 同级目录不再写配置**。

### 6.2 JSON Schema

```json
{
  "resolution": 300,
  "color_mode_ui": "彩色",
  "output_format": "jpg",
  "source": "平板",
  "duplex": false,
  "direct_save": false,
  "output_dir": "D:\\Scans",
  "extra_dirs": [],
  "disabled_ips": [],
  "selected_scanner_ip": "x.x.x.x",
  "saved_ips": [
    {"ip": "x.x.x.x", "model": "HP LaserJet MFP M232-M237"},
    {"ip": "x.x.x.x", "model": "HP LaserJet MFP M232-M237"}
  ],
  "nicknames": {
    "x.x.x.x": "财务室打印机",
    "x.x.x.x": "三楼 HP 7720"
  },
  "window_width": 1050,
  "window_height": 720,
  "window_x": 100,
  "window_y": 100
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `resolution` | int | `300` | 上次使用的扫描分辨率 |
| `color_mode_ui` | string | `"彩色"` | 上次使用的颜色模式（UI 显示值） |
| `output_format` | string | `"jpg"` | 上次使用的输出格式 |
| `source` | string | `"平板"` | 上次使用的来源（平板 / 输稿器） |
| `duplex` | bool | `false` | 上次是否勾选双面 |
| `direct_save` | bool | `false` | 上次是否启用"直存" |
| `output_dir` | string | `~/Documents/HP_Scans` | 扫描文件保存目录（`scan_coordinator.DEFAULT_OUT_DIR`） |
| `extra_dirs` | array | `[]` | 额外复制目录列表 |
| `disabled_ips` | array | `[]` | 被用户停用的设备 IP |
| `selected_scanner_ip` | string | — | 上次选中的设备 IP |
| `saved_ips` | array | `[]` | 已发现的打印机 IP 和型号 |
| `nicknames` | object | `{}` | 用户自定义名称，key=IP, value=名称 |
| `window_width` / `window_height` / `window_x` / `window_y` | int | `1050` / `720` / — / — | 主窗口位置与尺寸（恢复用） |
| `deskew_enabled` | bool | `true` | 纠偏总开关；关掉 ⇒ 只检测角度、不纠偏也不提醒（`save_manager.DESKEW_CONFIG_DEFAULTS`） |
| `deskew_apply_threshold` | float | `0.5` | 施加纠偏的最小角度（度）；低于此值不旋转 |
| `skew_warn_threshold` | float | `3.0` | 倾斜强提醒阈值（度）；绝对值严格大于才算「歪」 |
| `skew_warn_consecutive` | int | `3` | 连续几页都超阈值才发一次 `skew_warning` |
| ~~`theme`~~ | — | — | **v3.1 遗留**：全仓 `*.py` 搜 `theme` 零命中，v4.0 代码里既无主题开关也无此配置键 |

> **2026-10-21 订正**：原文的 schema 缺 `source` / `duplex` / `direct_save` / `extra_dirs` / `disabled_ips` / `selected_scanner_ip` / `window_*` 共 9 个**代码里真实存在**的键，且多了一个**代码里不存在**的 `theme` 键。以上键集合按 HEAD 时点（`hp_scan_gui_pyside.py` 的 `cfg[...]` 写点与 `cfg.get(...)` 读点）逐条核对。
>
> **2026-10-21 二次订正**：原文末行「⚠️ 在途的纠偏接入批次（尚未提交）还会新增 4 个纠偏相关键……落地后本表需再订正」——**该批次已提交并落地**（`c6037a4`「feat(skew): 自动纠偏接线」，已在 HEAD 之内）。4 个键（`deskew_enabled` / `deskew_apply_threshold` / `skew_warn_threshold` / `skew_warn_consecutive`）**已正式列入上表**，默认值取自 `save_manager.DESKEW_CONFIG_DEFAULTS`（唯一真源，`save_manager.py:101-87`）；`scan_coordinator.apply_deskew_defaults()`（`scan_coordinator.py:37`）在启动时把它们补进 `scan_config.json`。
>
> ⚠️ **仍存在的缺口**：这 4 个键目前**只有默认值，GUI 里没有开关可改** —— 用户只能改 `scan_config.json` 里的数值，界面不可调。

---

## 7. UI 规范

> ⚠️ **v3.1 遗留**：本节整体描述 v3.1 的 CustomTkinter UI（`[light│dark]` 切换、`gray55`/`gray40`、`CTkToplevel`、`messagebox.*`）。v4.0 已迁移到 **PySide6**（`hp_scan_gui_pyside.py`）：**无主题切换**、卡片配色取自 `hp_scan_gui_pyside.py:152 COLORS`、对话框用 `QDialog`、提示用 `QMessageBox.*`。以下条目凡点名 `ctk.*` / `CTk*` / `messagebox.*` 的，一律**以代码为准**。

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

> ⚠️ 顶栏的 `[light│dark]` 在 v4.0 **不存在**（无主题切换）。
> ⚠️ 左侧的 `[搜索局域网]` 在 v4.0 是「**刷新**」；右下角的 `[查询能力][预览状态]` 在 v4.0 是「**打印机状态**」—— **没有「查询能力」按钮**（能力是启动时后台探测、结果标在卡片标签上）。按钮全集见 §2.6.4。

### 7.2 卡片状态

| 状态 | 边框颜色(light/dark) | 边框宽度 |
|------|---------------------|---------|
| 未选中 | `gray55` / `gray40` | 1px |
| 选中 | `#3B8ED0` / `#1F6AA5` | 2px |

> ⚠️ `gray55` / `gray40` 是 CustomTkinter 的颜色名，v4.0 实际取自 `hp_scan_gui_pyside.py:152 COLORS`（单主题）。

### 7.3 对话框

| 对话框 | 类型 | 触发 | 内容 |
|--------|------|------|------|
| 能力探测结果（**无独立对话框**） | — | 启动时后台探测 / 点击「刷新」 | 结果直接显示在**扫描仪卡片的标签**上（型号/序列号/分辨率/颜色模式/格式/ADF/双面/平板），不弹窗 |
| 打印机状态 | showinfo | 点击"打印机状态" | State(Idle/Processing/Unknown) + ADF有纸(是/否) |
| 扫描失败 | showerror | 扫描异常 | 异常类型+消息+traceback + 排查提示 |
| 手动添加 | ~~CTkToplevel~~ → `QDialog` | 点击"手动添加" | IP输入框 + 探测按钮 + 结果反馈 |
| 重命名 | ~~CTkToplevel~~ → `QDialog` | 右键卡片 | 名称输入框 + 确认按钮 |

> **2026-10-21 订正**：原表两行点名 `CTkToplevel` —— 该类**在当前代码里不存在**（v4.0 用 `QDialog`，见 `hp_scan_gui_pyside.py:1698` / `:2200`）。`showinfo` / `askyesno` / `showerror` 同理，v4.0 是 `QMessageBox.information` / `question` / `critical`。
>
> **2026-10-21 二次订正**：
> - 原「能力查询结果 / showinfo / 点击"查询能力"」一行 —— 代码里**没有「查询能力」按钮**（按钮全集见 §2.6.4），能力是**启动时后台并行探测**（`hp_scan_gui_pyside.py:1156-1167` → 200ms 轮询 → `_rebuild_cards`）后直接标在卡片上，**不弹对话框**。已按现状改写。
> - 原「扫描完成 / askyesno / "已保存: {路径}" + "打开所在文件夹？"」一行 —— **代码里不弹这个确认框**：`hp_scan_gui_pyside.py:3346` / `:4045` 直接 `os.startfile(os.path.dirname(saved_files[0]))` 打开所在文件夹。全仓搜 `打开文件夹` / `askyesno` **零命中**；代码里仅有的 4 处 `QMessageBox.question` 是「清空配置」「至少保留一页」「保存覆盖确认」「清空历史」。该行已删除。
> - 「预览状态」按钮实际叫「**打印机状态**」，触发文案已同步订正。

### 7.4 通知方式

| 优先级 | 方式 | 场景 |
|--------|------|------|
| 信息 | 状态栏文字更新 | 发现完成、选中切换、ADF 自动切换 |
| 警告 | ~~messagebox.showwarning~~ → `QMessageBox.warning` | 未选打印机就点扫描/查询 |
| 错误 | ~~messagebox.showerror~~ → `QMessageBox.critical` | 扫描失败 |
| 进度 | 底部不确定进度条 | 搜索中、扫描中 |

---

## 8. 错误处理矩阵

### 8.1 eSCL 引擎错误

> ⚠️ **v3.1 遗留 + 2026-10-21 订正**：原表把异常类型写成裸 `RuntimeError` / `TimeoutError`。v4.0 引擎层改用**专用异常类**（都在 `escl_engine.py`，`ScanDeviceError` / `ScanCancelled` 均为 `RuntimeError` 子类）：`ScanDeviceError`（设备/协议/超时）、`ScanCancelled` 与 `ScanJobCancelled`（用户取消）。下表已按 HEAD 的实际 `raise` 点对齐。

| 异常类型 | 触发条件 | 处理方式 | UI 表现 |
|---------|---------|---------|--------|
| `ScanDeviceError: 未配置 eSCL URL` | escl_url 为空时调用 execute_scan | 抛出 | messagebox: "该打印机未探测到 eSCL，将尝试 WIA" |
| `ScanDeviceError: 创建扫描任务失败 HTTP {code}: {body[:300]}` | POST ScanJobs 返回非 2xx | 抛出 + 附响应体前 300 字符 | showerror: traceback |
| `ScanDeviceError: 无法获取 ScanJob URI` | 201 但无 Location 且响应体无 JobUri | 抛出 | showerror: traceback |
| `ScanDeviceError: 扫描数据已过期（HTTP 410），请放纸后重试` | NextDocument 返回 410 | 抛出 | showerror |
| `ScanDeviceError: {SCAN_TIMEOUT_MESSAGE}` | NextDocument 轮询超时 | 抛出 | showerror: 含排查提示 |
| `ScanCancelled` / `ScanJobCancelled` | 用户取消扫描（含 409 退避期间取消） | 抛出（语义=取消，不是失败） | 静默收尾，不弹错误框 |
| `requests.RequestException` | 网络超时/连接拒绝 | 轮询中静默忽略 | 继续下一轮 |
| `Exception` (fetch_capabilities) | XML 解析失败 | `pass` 吞掉 | 返回原 ScannerInfo 对象 |

### 8.2 GUI 错误

| 异常类型 | 处理方式 |
|---------|---------|
| 扫描线程内任何异常 | ⚠️ **v3.1 遗留**：原写 `traceback.format_exc()` → `_scan_done(False, tb)`；`_scan_done` 在 v4.0 **已不存在**（异常经 `ScanCoordinator` 的信号回主线程） |
| WIA 扫描失败 | 返回 None → showerror |
| 配置文件读写异常 | `try/except pass`，不影响程序运行（v4.0 改为 `config_io` 原子写 + 跨进程锁，失败记 error 日志） |
| 目录创建异常 | showerror: "无法创建目录" |

### 8.3 用户排查提示

> **2026-10-21 订正**：本节原写「扫描失败弹窗底部附排查指引」（eSCL 不通三步检查），但**代码里没有这段提示** —— 全仓 `*.py` 搜 `eSCL 不通` / `打印机已启用` / `浏览器访问` / `防火墙` **零命中**。下面按**代码现状**改写：代码给用户的排查文案是两套，写在异常消息里（`escl_engine`），由 GUI 的 `QMessageBox.critical` 直接展示。

扫描失败时实际弹出的排查文案（节选自 `escl_engine` 的异常消息）：

```
扫描超时：未能在限定时间内获取扫描数据。
请确认打印机已开机并完成初始化、面板上没有其它任务在执行；
若设备刚从休眠唤醒，请稍后重试。
```
（`escl_engine.SCAN_TIMEOUT_MESSAGE`，`escl_engine.py:833-837`；首页超时另有 `FIRST_PAGE_TIMEOUT_MESSAGE`，`:838-842`）

```
打印机当前不可用（HTTP 503，来源：平板）：设备尚未就绪，可能仍在收尾上一个扫描任务。请稍候重试（已自动重试 {retries} 次仍未成功）；若持续失败，请确认打印机面板上没有其它任务在执行。
```

```
打印机当前不可用（HTTP 503，来源：输稿器）：输稿器里没有纸，设备正在等纸（不是设备故障）。请往输稿器里放入纸张，放纸后请重试。
```
（`escl_engine._SERVICE_UNAVAILABLE_MESSAGE_PLEN` / `_SERVICE_UNAVAILABLE_MESSAGE_FEEDER`，`escl_engine.py:772-782`；**2026-10-21 二次订正**：原文只写了单套旧文案「打印机当前不可用（HTTP 503）。若选择的是输稿器(ADF)扫描…」，实际已按请求 XML 里的 `InputSource` **分流成平板/输稿器两套**——平板场景指向输稿器放纸是**指错方向**）

**代码里实际存在的另外几条错误文案**（`escl_engine.py`）：

| 场景 | 文案 |
|------|------|
| 未配置 eSCL URL | 「未配置 eSCL URL…」 |
| 拿不到 ScanJob URI | 「无法获取 ScanJob URI…」 |
| 扫描数据已过期（HTTP 410） | 「扫描数据已过期（HTTP 410），请放纸后重试」 |

> **未实现的承诺**：原三行 eSCL 排查指引（① 打印机已启用 eSCL/AirScan ② 浏览器访问 `http://打印机IP/eSCL/ScannerStatus` 确认可达 ③ 防火墙未拦截）**在代码里不存在**。若要补上，这是三条里对用户最有价值的一条（补进 `escl_engine` 约 10 行），但**本轮只订正文档、不改代码** —— 已列入「需补代码」清单交主控排期。

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
    ├── UDP 3702 探测有响应 (WSD) → WSD 可用 ✅（已实现：wsd_engine.py，UDP 3702 发现 / 5357 元数据）
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

产物：`dist\HP_Scan_Tool.exe`（**62.95 MiB** —— 2026-10-22 23:4x 用最终代码重打实测 **66,016,337 字节**；旧文档写的 "~22 MB" / "约 62 MB / 64,968,184 字节"分别是 **v3.1 打包时代**与 **2026-10-20 改造前基线**，"66,003,273 / 66,004,536 / 65,987,523 / 65,866,395 字节"是此前几次的包，均已过期）

### 10.2 开发运行

```bash
cd <PROJECT_DIR>
pip install -r requirements.txt
python hp_scan_gui_pyside.py
```

或双击 `run.bat`

### 10.3 分发方式

单文件便携 EXE，无需安装、无需管理员权限。拷贝到任意目录即可运行。配置文件在 **`%APPDATA%\HP_Scan_Tool\`** 自动生成（v4.0；v3.1 写在 EXE 同级目录）。

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
扫描完成 → 直接打开所在文件夹（os.startfile，无弹窗确认）
```

> **2026-10-21 订正**：流程图末行原写「扫描完成 → 弹窗 "打开文件夹？"」—— 代码里**没有这个弹窗**：`hp_scan_gui_pyside.py:3346` / `:4045` 保存后直接 `os.startfile` 打开所在文件夹。与 §7.3 对话框表、`HANDOVER.md` §4.1「保存后打开文件夹 | ✅ | os.startfile 直接打开，无弹窗确认」一致。

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
| 39 | 07-16 | **自动曝光功能** | 扫描件灰底、对比度低 | PC 端后处理：自动模式=高斯模糊除法去灰底，手动模式=曝光滑块（`ExposurePanel`），预览与落盘链路均生效（2026-10-20 并回本项目） |

---

## 13. 已知限制

| # | 限制 | 影响 | 优先级 | 备注 |
|---|------|------|--------|------|
| 1 | ~~单线程扫描，不支持并发~~ | **2026-10-21 起已支持多台并发**（`c5de886`）：GUI 扫描期间不再禁用扫描按钮，在途 job 表 `_active_jobs` 逐台跟踪，每台各自可取消、各自回填结果。⚠️ 双面/续扫（会话）模式仍保持串行 | P2 | 已解除该限制（会话模式除外） |
| 2 | ~~不支持 WSD 协议~~ | ~~WIA 兜底需要 Windows 识别到设备~~ | ~~P3~~ | ✅ 已实现：wsd_engine.py（UDP 3702 发现 / 5357 元数据），CLI `discover --wsd-only` 与默认双协议路径均已接线 |
| 3 | WIA 仅 USB | 网络扫描仪 WIA 不可用 | P2 | Windows WIA 栈限制 |
| 4 | ~~M232 ScannerCapabilities 走 HTTPS 301~~ | ~~自签名证书导致能力查询失败~~ | ~~P2~~ | ✅ 已修复 (Bug #11)：全部请求加 verify=False |
| 5 | 不支持扫描预览 | 扫描前无法预览 | P3 | 需要 eSCL Preview 端点 |
| 6 | ~~无扫描历史记录~~ | ~~扫描完成后无日志~~ | ~~P3~~ | ✅ 已实现：`history_manager.py`（`history.json`，GUI 与 CLI 都在用） |
| 7 | ~~不支持多页 PDF~~ | ~~ADF 扫描仍导出单页~~ | ~~P2~~ | ✅ 已实现：`save_manager.save_pages()` / `_write_pdf()` 本地合成多页 PDF（不再依赖打印机直出 PDF） |
| 8 | 手动双面**只支持输稿器（ADF）** | 平板没有背面可用 | P2 | 手动双面 = 两趟单面 + 本地合并（§2.4.6） |
| 9 | 断点续扫**依赖磁盘缓存** | 缓存过期（`cache_config.json` 的 `stale_hours`，默认 24 小时）后页会被清掉，隔天续扫可能丢页 | P2 | 页的物理载体是缓存目录（§2.4.7） |
| 10 | 纠偏阈值**不可在界面调整** | 只能用配置里的默认值 | P3 | `deskew_enabled` 等 4 个键只有默认值（§2.4.9） |
| 11 | 批次 4 的四项能力**只有 GUI 入口** | CLI 无双面 / 续扫 / 合并 / 纠偏参数 | P3 | `cli.py` 仍是 scan / discover / status / history 四个子命令 |

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
| ⚠️ 主题切换 | ~~light→dark→light~~ | ~~全局控件联动，无低对比度~~ —— **v3.1 遗留**：v4.0 无主题切换（见 §2.6.2） |
| 目录选择 | 浏览 → 选 D:\test → 退出重启 | 保存目录仍为 D:\test |
| 分辨率切换 | 选 600DPI → 扫描 | 生成高分辨率图片 |
| PDF 格式 | 选 PDF → M232 扫描 | 本地合成为 PDF（`save_manager._write_pdf()`；不再由引擎转码，见 §2.4.3） |
| 扫描失败 | 断开网线 → 扫描 | 弹窗显示 `ScanDeviceError` + 排查指引 |
| 手动双面 | 模式选「双面（手动）」→ 扫正面 → 整叠纸翻面放回输稿器 → 再扫背面 | 状态栏提示"正面已扫 N 页，请翻面放回后再次点击「扫描」"；背面扫完弹合并预览，页序为 F1/B末、F2/B末-1… |
| 续扫合并 | 模式选「加入现有文档（续扫）」→ 扫 3 页 → 再扫 2 页 → 点「结束并保存」 | 合并预览显示共 5 页；保存后得到**一个** PDF，页序 = 批序 + 批内原序 |
| 断点续扫 | 扫描中触发「中断并从第 N 页重扫」 | 前 N-1 页保留、第 N 页及之后丢弃（含缓存文件），状态栏提示"已中断扫描，将从第 N 页重扫" |
| 倾斜强提醒 | 连续多页摆放明显歪斜 | 弹「页面倾斜过大」弹窗（带缩略图），三个选项：继续扫描（自动纠偏）/ 中断并从第 N 页重扫 / 强制保存（不纠偏）；强制保存的页落盘不纠偏 |

### 13.2 边界测试

| 场景 | 预期 |
|------|------|
| 无网络 | 启动不崩溃，列表为空，状态栏"未发现打印机" |
| 配置文件损坏 | 静默降级为默认配置 |
| EXE 移动位置 | ⚠️ v3.1 写"配置丢失"—— **v4.0 已改**：配置在 `%APPDATA%\HP_Scan_Tool\`，移动 EXE **不丢配置** |
| 长打印机名称 | wraplength=260 自动换行 |

---

## 15. 未来规划

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P1 | ~~扫描历史记录~~ | ✅ 已实现（`history_manager.py`）—— 列表展示最近扫描，支持一键打开 |
| P1 | ~~多页 PDF 合成~~ | ✅ 已实现（`save_manager._write_pdf()`）—— ADF 多页扫描 → 本地合并为单 PDF |
| P2 | 扫描预览 | 低分辨率预览 → 确认 → 正式扫描 |
| P2 | ~~WSD 协议支持~~ | ✅ 已实现（wsd_engine.py，UDP 3702 发现）—— 扩展兼容老旧打印机 |
| P2 | 扫描区域裁剪 | 手动指定扫描区域 |
| P3 | 批量扫描 | 多台打印机并发 |
| P3 | ~~命令行模式~~ | ✅ 已实现（`cli.py`，子命令 `scan` / `discover` / `history`；⚠️ 形态是 `python cli.py scan --ip ...`，不是 `HP_Scan_Tool.exe --scan`） |
| P3 | macOS 支持 | 跨平台移植（需替换 WIA 为 ICA） |
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
