# HP Scan Tool — 领域术语表

> ⚠️ **2026-10-20 起，曝光/曝光预设已并回本项目**：放弃独立 DocFix 软件，基础曝光/滤镜在本项目内闭环。依据 `docs/adr/0008-exposure-enhancement.md` + `docs/design/2026-10-20_新功能调研与方案_双面_续扫_纠偏_曝光_v1.0.md` §0 需求 F。现状：`exposure.py` / `preset_manager.py` 已入库，`ExposurePanel` 已接入预览 GUI，曝光已接进落盘链路（`save_manager.py`），预设持久化于 `%APPDATA%\HP_Scan_Tool\presets.json`。
> （历史：v3.3—2026-10-20 期间相关定义曾随 DocFix 剥离，该表述于 2026-10-20 作废。）

## 核心概念

**eSCL (AirScan)**
HP 网络扫描协议，基于 HTTP+XML。扫描仪通过 mDNS 注册服务，客户端通过 REST 式 API 交互：ScannerCapabilities → ScannerStatus → ScanJobs → NextDocument。

**WIA (Windows Image Acquisition)**
Windows 本地扫描驱动接口，作为 eSCL 不可用时的降级方案。仅支持 USB 连接设备。

**ScannerInfo**
扫描仪的完整信息载体：IP、端口、eSCL URL、能力参数（分辨率/颜色模式/格式/ADF/双面）。

**ScanJob**
一次扫描任务的生命周期：POST 创建 → 轮询状态 → GET NextDocument 获取图像数据。

**能力已知 (capabilities_known)**
`ScannerInfo` 上的布尔字段：**设备到底有没有 ADF 的唯一权威信号**。
只有 `fetch_capabilities()` 真的拿到能力文档（HTTP 200 + XML 可解析）才置位；
走 `ScannerStatus` 回退探测**不置位**（那是「猜」，不是「知道」）。
⚠️ 旧的「能力字段指纹前后对比」启发式已废弃 —— 在 WSD / GUI 链路上它是幂等 no-op，
会把真机误判成「能力未知」从而丢掉自动回退。

**命名形态 (Naming)**
跨入口统一的产物/任务命名：`{前缀}_{秒级时间戳}_{字母开头的短随机后缀}`。
★ 唯一性来自 `uuid4()`（OS CSPRNG），时间戳只做可读性前缀；
★ **秒级而非微秒** —— Windows 时钟粒度 15.625 ms，同 tick 内微秒位本来就相同，
属于**虚假精度**；★ 首字符固定是字母 ⇒ 不会长得像 `_001` 组号。

## 发现与连接

**mDNS 发现**
通过 zeroconf 监听 `_uscan._tcp.local.` 和 `_scanner._tcp.local.` 服务，自动发现局域网内的 eSCL 扫描仪。

**探活 (Probe)**
向指定 IP 发送 HTTP 请求检测 eSCL 服务是否可用，返回基础 URL（`/eSCL/` 或 `/`）。

> ⚠️ **2026-10-21 订正（v4.0 口径）**：v3.1 时代 GUI 有「载入已保存打印机 → 后台逐台探活 → 每探通一台就刷一次卡片」那套逻辑（`_restore_presets` / `_probe_saved`），**v4.0 的 PySide6 GUI 已无这两个方法** —— 发现统一走 `ScanApp._auto_discover()` → `coordinator.discover_scanners_background(...)` → `_on_discover_finished()` → `_rebuild_cards()`；`saved_ips` 现在只用于「手动添加」时补写。`PRD.md` §2.2.4 已把这段标为 v3.1 遗留。

**能力查询 (Capabilities)**
从 `/eSCL/ScannerCapabilities` XML 解析扫描仪支持的全部参数：分辨率列表、颜色模式、输出格式、ADF/双面支持等。

## 扫描参数

**来源 (Source / InputSource)**
扫描输入来源：平板 (Platen) 或 输稿器 (Feeder/ADF)。

**颜色模式 (ColorMode)**
RGB24（彩色）、Grayscale8（灰度）、BlackAndWhite1（黑白）。

**输出格式 (DocumentFormat)**
扫描数据的 MIME 格式：image/jpeg、image/png、image/tiff、application/pdf。

**曝光 (Exposure)**
落盘前的图像曝光/对比度调整（`exposure.py`，纯 PIL）。
★ 2026-10-20 起**已并回本项目**（v3.3—2026-10-20 期间曾随 DocFix 剥离）。
落盘链路顺序：**曝光 → 旋转 → A4 归一**；`jpg`/`png`/`tiff` 产物**也**过 `_a4_canvas`
⇒ 同一页的图片与 PDF 尺寸必然一致。
★ **CLI 例外**（2026-10-21）：`cli.py scan` 的**图片**路径走**字节级曝光**
——只改像素，**不做 A4 归一、不做纠偏**（改造前 CLI 图片就是字节原样落盘，
本就不是 GUI 那条路径；这是既有差异，不是新引入的）；CLI 的 **PDF** 产物
仍走 `save_manager` ⇒ 与 GUI 一致。`cli.py scan --exposure <预设名>` 可用
内置 4 个预设或 `%APPDATA%\HP_Scan_Tool\presets.json` 里的自定义预设；
历史里记录的曝光参数是**净化后**的值（= 像素实际生效的那份）。

**预设 (Preset)**
一组扫描/曝光参数的可复用组合（`preset_manager.py`），内置 4 个 + 用户自定义，
持久化于 `%APPDATA%\HP_Scan_Tool\presets.json`。

**倾斜角与纠偏 (Skew / Deskew)**
`skew_detect.py`（纯 PIL 投影法，零第三方依赖）在**每页写入缓存后**测一次倾斜角，
写进 `page_meta.deskew_angle`（**绝不旋转缓存原件**）；落盘时 `save_manager` 按阈值
（默认 0.5°）施加纠偏。★ 阈值相关 4 个配置键见「持久化」段的 v4.0 口径注。

## 持久化

**配置 (Config)**
scan_config.json 文件，保存用户偏好：分辨率、颜色模式、格式、来源、已保存的打印机 IP 列表和自定义名称。

> ⚠️ **2026-10-21 订正（v4.0 口径）**：完整键表见 `PRD.md` §6.2。除上面列的以外，v4.0 实际还会写 `duplex` / `direct_save` / `extra_dirs` / `disabled_ips` / `selected_scanner_ip` / `window_width|height|x|y`，以及 4 个纠偏相关键 `deskew_enabled` / `deskew_apply_threshold` / `skew_warn_threshold` / `skew_warn_consecutive`（启动时由 `scan_coordinator.apply_deskew_defaults()` 自动补进文件）。**没有 `theme` 键** —— v4.0 是单主题，全仓搜 `theme` 零命中。

**自定义名称 (Custom Name)**
用户为打印机设置的别名，按 IP 关联，覆盖默认的设备名称。

**配置文件 (Profile)**
按设备 IP 保存的独立扫描参数（分辨率/颜色/格式/来源），切换设备时自动加载。

## 缓存与并发

**磁盘缓存 (Disk Cache)**
扫描数据直接写入 `{文档目录}/HP_Scans/.cache/{job_id}/`，不再全量驻留内存。预览时加载缩略图，确认保存时复制到目标位置后清除缓存。

**缓存清理 (Cache Eviction)**
上限可配置（默认 1024MB），达到 95% 触发线时自动 FIFO 清除 150MB。启动时清理超过 24 小时的残留缓存。

**并发扫描 (Concurrent Scanning)**
⚠️ **后端支持、GUI 未开放** —— 后端 `ScanCoordinator` 用 `QThreadPool`（`setMaxThreadCount(4)`）可同时跑多个任务、每个任务独立缓存子目录；但 GUI 在扫描期间禁用「扫描」按钮（`scan_btn.setEnabled(False)`）且只跟踪单个 `_current_job_id` ⇒ **界面上一次只能扫一台**。这条与 `PRD.md` §13「单线程扫描，不支持并发」口径一致。

## 发现协议

**WSD (Web Services Discovery)**
微软推的设备发现协议，SOAP over WS-Discovery。与 mDNS 并行运行，结果按 IP 去重合并，最大化发现率。实现在 `wsd_engine.py`。

## 多页扫描

**扫描会话 (Document Session)**
一次「结束并保存」之前累积的所有页的集合（`document_session.py`）：多页、
插入/移除分割线分组、双面续扫、中途从第 N 页重扫。
★ 落盘时按组产出 `{base}.pdf` / `{base}_001.pdf` …；
★ 会话状态采用**三方合并**（磁盘 / 进程内 / 用户上次已保存）⇒ 并发保存不丢页，
也不会让已删的页复活。

**批量预览 (Batch Preview)**
多页 ADF 扫描全部完成后统一预览，用户可翻页查看/删除/调整，最后一次性保存。

**页面分组 (Page Grouping)**
PDF 格式支持自定义页面分组保存：用户可在页面间插入分割线，每组生成独立 PDF 文件。

## 平台抽象

**资源路径 (Resource Path)**
开发模式下从 `resources/` 目录加载资源，打包模式下从 `sys._MEIPASS` 加载。实现在 `get_resource_path()` 函数。

## 命令行

**CLI**
argparse 子命令模式：`hp_scan scan`、`hp_scan discover`、`hp_scan status`、`hp_scan history`。无 GUI 扫描入口，适合脚本集成。

## 扫描历史

**Scan History**
每次扫描完成后记录元数据到 `history.json`（时间/设备/页数/路径/参数），GUI 提供历史列表。

## 监听模式

**Listen Mode**
等待用户在打印机面板上选择「扫描到计算机」，打印机主动创建扫描任务，计算机轮询获取。实现在 `listen_engine.py`。
