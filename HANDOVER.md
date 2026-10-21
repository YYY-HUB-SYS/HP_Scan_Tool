# HP Scan Tool — 项目交接文档

> 日期: 2026-09-21 | 当前版本: v4.0（PySide6 重写版）

---

## 1. 项目概况

HP Scan Tool 是一款面向惠普网络打印机的 Windows 桌面扫描客户端，通过 eSCL/AirScan 协议实现免驱网络扫描，WIA 作为降级方案。

**项目路径**: `<PROJECT_DIR>`
**Git 仓库**: 已初始化（`.git` 存在），remote 指向 <GIT_REPO>
**技术栈**: Python 3.11+ / PySide6 / Pillow / python-zeroconf / WSDiscovery / requests / pywin32
**打包**: 两种形态 —— **安装包**（`build_installer.py` → PyInstaller **onedir** + Inno Setup，per-user 免管理员，★ 正式部署形态，**运行期不解压 `%TEMP%`**）与**单文件便携版**（`build_exe.py` → PyInstaller onefile，免安装但每次启动解压 138.5 MiB 到 `%TEMP%`）

### 1.1 当前状态（2026-09-21 v4.0）

HP Scan Tool 已稳定为**纯扫描工具**，职责边界清晰：

- 发现设备（mDNS + WSD 双协议）
- 选择扫描仪、配置参数（分辨率/颜色/格式/来源）
- 执行扫描（平板单页 / ADF 多页）
- 缓存 → 预览 → 保存原始文件
- 记录扫描历史

**2026-10-20 起曝光/预设已并回本项目**（放弃独立 DocFix 软件，基础曝光/滤镜在本项目内闭环）：`exposure.py` / `preset_manager.py` 已入库，`ExposurePanel` 已接入两个预览对话框，曝光已接进落盘链路（`save_manager.py`），预设持久化于 `%APPDATA%\HP_Scan_Tool\presets.json`。直方图未实现、未纳入范围；扶正（自动纠偏）引擎**已完成并已接入扫描流水线**（`c6037a4`）：每页写入缓存后调 `skew_detect.estimate_skew` 测倾斜角并写入 `page_meta.deskew_angle`，落盘时按阈值施加纠偏。`skew_detect` 被 `scan_coordinator.py` / `save_manager.py` **静态 import** ⇒ PyInstaller 依赖分析实测 `skew_detect` FOUND ⇒ **随 EXE 交付**。⚠️ 但 **GUI 里没有纠偏开关**（`deskew_enabled` 等 4 个键只有默认值，界面不可改）。

**2026-10-21 批次 4 新增（文档会话类功能，均已在 HEAD 内）**：以 `document_session.py`（会话账本）+ `doc_merge.py`（页序算法）+ GUI 会话接线为准，四项用户可见能力：

| 能力 | 落点 | 一句话行为 |
|------|------|-----------|
| 手动双面扫描 | `document_session.py`（`side` / `SIDES`）+ `doc_merge.interleave_duplex` + `hp_scan_gui_pyside.py` 的 `_pending_side_by_job` / `_session_batch_finished` | 两趟单面（正面一趟、翻面背面一趟）⇒ 交错合并成一份文档；**只支持输稿器（ADF）** |
| 断点续扫 | `resume_from_page` / `DocumentSession.resume_scanning` / GUI「从第 N 页重扫」 | 保留前 N-1 页、丢弃第 N 页及之后；前提是设备仍可用、页还在缓存里 |
| 大批量合并成单个 PDF | `save_manager.save_session`（B4-5）+ `doc_merge.merge_batches` / `interleave_duplex` | 多批扫描合并成一个 PDF（页序规则见 §4.4） |
| 自动纠偏 + 倾斜强提醒 | `skew_detect.estimate_skew` + `hp_scan_gui_pyside.SkewWarningDialog` | 每页测倾斜角；连续超阈值弹**可中断 / 可续扫 / 可强制保存**的强提醒；落盘按阈值施加纠偏 |

⚠️ 这四项**只走 GUI**（CLI 无对应子命令）；⚠️ 界面里**仍然没有纠偏开关**（`deskew_enabled` 等 4 个键只有默认值）—— 与上一段的结论一致，未变。

---

## 2. 文件结构

```
<PROJECT_DIR>\
├── hp_scan_gui_pyside.py   # GUI 主程序 — ScanApp + PreviewDialog + MultiPagePreviewDialog + ScannerCard + HistoryDialog
├── escl_engine.py          # eSCL/AirScan 协议引擎 — 发现/探活/能力查询/扫描执行
├── wia_engine.py           # WIA 降级引擎 — 本地 USB 扫描仪
├── wsd_engine.py           # WS-Discovery 引擎 — 并行于 mDNS 的第二种发现方式
├── listen_engine.py        # 监听模式引擎 — 等待打印机面板触发扫描
├── scan_coordinator.py     # 扫描编排器 — 配置持久化/扫描仪管理/扫描执行/缓存协调
├── cache_manager.py        # 磁盘缓存管理 — FIFO 驱逐/活跃任务保护/自动清理
├── history_manager.py      # 扫描历史日志 — JSON 格式记录
├── profile_manager.py      # 扫描配置文件管理器 — 按设备 IP 保存独立参数
├── cli.py                  # CLI 命令行入口 — scan/discover/status/history 子命令
├── save_manager.py         # 保存落盘层 — 曝光施加/分组命名/写历史/清缓存
├── page_meta.py            # 页元数据模型 — 页序号/角度/分组等会话级元数据
├── document_session.py     # 文档会话 — 双面/续扫的会话账本 + 状态机 + 会话文件存储
├── doc_merge.py            # 页序合并算法 — 双面交错 / 批次拼接（纯函数，零 IO 零 Qt）
├── config_io.py            # 配置读写 helper — 原子写 + 跨进程锁
├── exposure.py             # 曝光引擎 — off/auto/manual（2026-10-20 并回本项目，在用）
├── preset_manager.py       # 曝光预设管理 — 内置 4 个 + 用户自定义（2026-10-20 并回本项目，在用）
├── skew_detect.py          # 倾斜角检测引擎 — 纯 PIL 投影法（✅ 已接入扫描流水线 ⇒ 随 EXE 交付）
├── build_exe.py            # PyInstaller 打包脚本
├── resources/              # 运行时资源（图标/字体/样式，随 EXE 打包）
│   ├── fonts/
│   │   └── NotoSansSC-Regular.ttf
│   ├── icons/
│   │   ├── app.ico
│   │   ├── icon_16.png
│   │   ├── icon_32.png
│   │   └── icon_64.png
│   └── styles/
│       └── app.qss
├── tests/                  # pytest 测试（2026-10-21 11:1x 实测 79 个 .py：conftest.py + 75 个 test_*.py + 3 个测试辅助模块）
│   ├── conftest.py
│   ├── cli_subprocess_probe.py   # CLI 子进程测试用探针（辅助模块，非测试）
│   ├── skew_fixtures.py          # 倾斜检测测试用夹具（辅助模块，非测试）
│   └── test_*.py                 # 75 个测试模块（缓存 / 历史 / CLI / eSCL / 曝光 / 预设 / 倾斜检测 /
│                                 #   文档会话 / 双面合并 / GUI 一致性 / 协调器 / 落盘 …）
├── docs/                     # 项目文档
│   ├── SPEC-v3.3.md              # v3.3 规格文档（User Stories + 技术方案）
│   ├── tickets.md                # 实施票据 T1-T10（含依赖图）
│   ├── adr/                      # 架构决策记录（12 份 ADR）
│   │   ├── 0001 ~ 0011           # 格式映射/多页/缓存/WSD/CLI 等
│   │   └── 0012-platform-abstraction-runtime.md  # 平台抽象层（未实现）
│   ├── audit/                    # 全量代码审计 / 审计发现修复计划（3 份）
│   ├── design/                   # 新功能调研与方案 / 选型实测（3 份）
│   ├── prototype/
│   │   └── multipage-preview.jsx # 多页预览 UI 原型（React）
│   ├── research/
│   │   └── wsd-discovery.md      # WSD 发现协议调研
│   ├── superpowers/              # 本轮设计规格与实施计划（plans / specs）
│   └── verify/
│       ├── baseline_D1.md        # 真机基准（改造前）+ D2 回归章节的索引
│       └── regression_D2.md      # 真机回归对照（改造后）
├── tools/
│   ├── verify_scan.py      # 真机基准脚本（D1/D2）
│   └── verify_release.py   # 出包自检（onedir 完整性 + 安装包 PE/版本 + 清单报告）
├── installer/
│   ├── hp_scan_tool.iss    # Inno Setup 安装脚本（per-user，免管理员）
│   └── Languages/
│       └── ChineseSimplified.isl  # 仓库自带中文向导（换机/CI 不依赖系统安装）
├── build_installer.py      # 打安装包：onedir → ISCC → 自检（一条命令）
├── PRD.md                  # 产品需求文档 v3.1
├── CONTEXT.md              # 领域术语表
├── CODEBUDDY.md            # CodeBuddy 项目指引
├── README.md               # 项目入口文档
├── AGENTS.*.md             # 接手本项目的 agent 规则
├── requirements.txt        # Python 依赖
├── deploy-config.example.md # 部署配置示例
├── run.bat                 # 开发模式启动脚本
├── backup.bat              # 下班存档脚本
├── .gitignore              # Git 忽略规则
├── .github/workflows/build.yml # CI：test → build → release
└── LICENSE
```

> 运行时配置文件（`scan_config.json` / `cache_config.json` / `history.json` / `profiles.json` / `presets.json`）**不在仓库内**，统一落在 `%APPDATA%\HP_Scan_Tool\`。

---

## 3. 架构概览

### 3.1 当前架构（v4.0 PySide6）

```
hp_scan_gui_pyside.py (GUI 层 — PySide6)
  ├── ScanApp                  — 主窗口：扫描仪管理 + 参数配置 + 扫描启动
  ├── LoadingDialog            — 启动进度对话框
      ├── ScannerCard              — 扫描仪卡片组件（选中/重命名/禁用）
  ├── ImageViewer              — 图片预览（缩放/旋转）
  ├── AnimatedButton           — 按压动画按钮
  ├── PreviewDialog            — 单页预览 + 文件名确认 + 保存
  ├── MultiPagePreviewDialog   — 多页预览：缩略图 + 分组 + 批量保存
  └── HistoryDialog            — 扫描历史浏览器

scan_coordinator.py (业务层 — 事件驱动，依赖 PySide6.QtCore)
  └── ScanCoordinator(QObject) — 扫描仪发现/探活/扫描执行/缓存协调
      ├── 信号 scan_started/progress/finished/failed/cancelled
      └── QThreadPool 任务层，run_scan_sync 为同步可测体

save_manager.py (I/O 层 — 纯 Python，不依赖 GUI)
  └── save_pages / save_single — 落盘/分组命名/额外目录/清缓存/写历史

escl_engine.py (协议层)        — eSCL/AirScan 完整实现
wia_engine.py (协议层)         — WIA 降级方案
wsd_engine.py (发现层)         — WS-Discovery 发现
listen_engine.py (监听层)      — 等待打印机面板触发扫描
cache_manager.py (I/O层)       — 磁盘缓存管理
history_manager.py (I/O层)     — 扫描历史日志
profile_manager.py (I/O层)     — 按设备保存扫描参数
cli.py (CLI入口)               — argparse 子命令: scan/discover/status/history
```

### 3.2 数据流

```
扫描仪 → eSCL/WIA 引擎 → 原始 bytes → cache_manager 写入磁盘
                                          ↓
                                  PreviewDialog 从缓存加载
                                          ↓
                                  用户确认 → 保存文件 → 打开文件夹
                                          ↓
                                  cache_manager 清理缓存
```

### 3.3 后处理架构（2026-10-20 起已并回本项目）

```
exposure.py (在用)
  ├── _auto_enhance()           — 高斯模糊除法文档增强（去灰底）
  ├── apply_exposure()          — 统一曝光入口（off/auto/manual）
  ├── _gamma_lut()              — Gamma 校正 LUT
  ├── _shadow_highlight_lut()   — 阴影/高光分离 LUT
  └── auto/manual_exposure()    — bytes 向后兼容接口

preset_manager.py (在用)
  ├── _BUILTIN_PRESETS          — 4 个内置预设（文字增强/照片还原/去灰底/旧文件修复）
  ├── load_presets()            — 加载内置+用户预设
  ├── save/delete_user_preset() — 用户预设管理
  └── export/import_preset()    — 预设导入导出
```

> **现状（2026-10-20）**：上述两个模块**已在扫描链路中启用** —— `hp_scan_gui_pyside.py` 静态 import 并挂出 `ExposurePanel`（单页/多页预览对话框各一个），`save_manager.py` 在落盘前施加曝光；预设持久化于 `%APPDATA%\HP_Scan_Tool\presets.json`。

---

## 4. 已完成的功能清单

### 4.1 P0 核心功能（全部完成）

| 功能 | 状态 | 说明 |
|------|------|------|
| eSCL 自动发现 | ✅ | mDNS 监听 `_uscan._tcp.local.` + `_scanner._tcp.local.` |
| WSD 发现 | ✅ | WS-Discovery 并行发现，按 IP 去重合并 |
| IP 探活 + 能力查询 | ✅ | HTTP GET `/eSCL/ScannerCapabilities` XML 解析 |
| 平板单页扫描 | ✅ | POST ScanJobs → GET NextDocument |
| ADF 多页扫描 | ✅ | 自动检测 ADF 有纸 → 切换 Feeder 模式 |
| 手动添加打印机 | ✅ | 输入 IP → 自动探测 eSCL |
| 磁盘缓存 | ✅ | `{Documents}/HP_Scans/.cache/{job_id}/` |
| 产物文件名 | ⚠️ 自动生成、**不可改** | 文件名由 `naming` 模块按「秒级时间戳 + 字母开头随机后缀」自动生成（`{prefix}_{YYYYmmdd}_{HHMMSS}_{唯一成分}.{ext}`），GUI **没有**让用户改文件名/路径的对话框。★ **2026-10-21 订正**：原文写「文件名确认对话框 ✅ 支持改文件名和路径」是**错的** —— 全仓无此对话框（`PreviewDialog` / `MultiPagePreviewDialog` 都没有）。多组时能给**每组起名**（组名落进文件名），见 §4.2「多页分组管理」 |
| 保存后打开文件夹 | ✅ | os.startfile 直接打开，无弹窗确认 |
| 扫描历史 | ✅ | JSON 日志，GUI 历史对话框 |
| CLI 模式 | ✅ | scan/discover/status/history 四个子命令 |
| WIA 降级 | ✅ | eSCL 不可用时自动降级到 WIA |
| 监听模式 | ✅ | 等待打印机面板触发扫描 |

### 4.2 P1 体验优化（除标注为 v3.1 遗留的条目外，均已完成）

| 功能 | 状态 | 说明 |
|------|------|------|
| 深色/浅色主题 | ⚠️ v3.1 遗留 | **v4.0 无主题机制** —— 全仓 `*.py` 搜 `theme` 零命中，GUI 是单主题（`hp_scan_gui_pyside.py:152 COLORS` + `resources/styles/app.qss`），既无切换控件也无 `theme` 配置键。PRD §2.6.2 已把整节标为 v3.1 遗留 |
| 打印机自定义名称 | ✅ | 按 IP 关联，配置持久化 |
| 打印机优先级排序 | ✅ | ▲▼ 按钮调整顺序，重启后记住 |
| 扫描仪选中持久化 | ✅ | 按 IP 恢复上次选中的扫描仪 |
| 任务栏图标清晰 | ✅ | PySide6 实际走 `QIcon`：`get_resource_path("icons/icon_64.png")`（`hp_scan_gui_pyside.py:203`）。⚠️ `iconphoto` 是 CustomTkinter 的 API，v4.0 已不存在 |
| 任务栏图标不闪烁 | ⚠️ v3.1 遗留 | v4.0 **没有** `SetCurrentProcessExplicitAppUserModelID` 调用（全仓零命中），未做显式任务栏分组设置 |
| 窗口防逐帧渲染 | ✅ | withdraw → build → update_idletasks → deiconify |
| 嵌入式字体 | ✅ | AddFontResourceExW 私有加载，ClearType 优化 |
| 多页分组管理 | ✅ | 插入分割 / 移除分割（`hp_scan_gui_pyside.py`「＋分组线 / －分组线」按钮）。★ **「重命名分组」已实现**（2026-10-21）：多页预览的**缩略图列里，每组起始页之前一个组名输入框**（第 1 组在第 1 页之前，第 2..N 组在分隔线之后）；组名经 `save_manager.sanitize_group_name` 净化（Windows 非法字符 / 路径分隔符 / 控制字符 / 结尾的点与空格 / 超长）后**落进产物文件名**（`{base}_{组名}.pdf` / `{base}_{组名}_{组内号:03d}.{ext}`）。★ **留空 ⇒ 回落到组号** ⇒ 文件名与改造前**逐字节一致**；**单组**只有 1 个输入框。★ 保存前**不再弹任何命名对话框** |
| 逐页曝光覆盖 | ✅ | ★ **2026-10-21 已实现**（用户拍板要做）。多页预览里当前页可**单独设置曝光**：「本页单独设置」以整批当前曝光为初值建覆盖，「跟随整批」清除覆盖；预览按**当前页的覆盖**渲染；保存时按 `page_specs` 下标对齐传给 `save_manager`（`page_exposures`：元素 `None` ⇒ 跟随整批，dict ⇒ 该页单独用自己的曝光）。落盘侧 PDF 与图片两条路径都支持（`save_manager.py` 的 `save_pages(..., page_exposures=...)` / `SaveRequest.page_exposures`）。★ 语义坑：`ExposurePanel.exposure_dict()` 在 mode=off 时返回 `None`，而 `None` 在落盘侧表示「跟随整批」⇒ 「本页明确不套曝光」必须存**显式的** `{"mode": "off", ...}`。⚠️ 会话（双面/续扫）落盘**不引入**逐页曝光；历史记录里的曝光仍是文档级那一份 |
| 页面删除 | ✅ | 多页预览中删除不需要的页面 |
| 并发扫描 | ✅ 后端支持 / **GUI 已开放多台并发** | 后端 `scan_coordinator` 用 `QThreadPool` 且 `setMaxThreadCount(4)`（`scan_coordinator.py:382`）。★ **2026-10-21 起 GUI 也开放了**（`c5de886`）：扫描期间不再禁用扫描按钮，改用在途 job 表 `_active_jobs`（job_id → **该 job 自己的设备**）跟踪多台 ⇒ 每台各自可取消、各自回填结果与进度；状态栏仍只放**一个**取消按钮，取消**最近启动**的那台（再点一次轮到上一台）⇒ 每台都能单独取消。⚠️ **双面/续扫（会话）模式仍保持串行**（会话本质是顺序的：正面→背面、逐批追加，并行会把记账搞乱）⇒ 该模式下按钮仍禁用并拒绝开第二批。⚠️ 已知不足：① 曝光参数仍是全局态（并发下 B 台启动会清掉 A 台残留曝光，未改）；② 预览对话框仍是模态 `exec()`，A 台完成时弹预览会挡住主窗口（B 台仍在后台扫） |
| ADF 自动检测 | ✅ | 扫描前查询 adf_loaded，自动切换平板/输稿器 |

### 4.3 架构分离（2026-07-19 完成，**2026-10-20 已回退**）

| 操作 | 详情 |
|------|------|
| 剥离 exposure.py | 从各模块中移除所有曝光调用 —— **2026-10-20 已回流**（见下） |
| 剥离 preset_manager.py | 不再被任何活跃模块导入 —— **2026-10-20 已回流** |
| 简化 PreviewDialog | 去掉曝光调整/直方图/预设，只保留预览+保存 —— **2026-10-20 已恢复曝光面板（`ExposurePanel`）** |
| 简化 MultiPagePreviewDialog | 去掉曝光控制/逐页覆盖，保留缩略图/分组/保存 —— **2026-10-20 已恢复曝光面板**；**2026-10-21 已补回「逐页曝光覆盖」**（见 §4.2） |
| 清理 spec 文件 | 移除 exposure/preset_manager/ImageFilter/ImageMath —— **2026-10-20 已回流**（`exposure.py` 用到 `ImageFilter` / `ImageMath`） |
| 清理测试 | 移除 conftest.py 和 test_cli.py 中的曝光相关内容 —— **2026-10-20 已补回**（`tests/test_exposure.py` / `test_exposure_integration.py` / `test_preset_manager.py`） |
| 备份到 _docfix_reserve/ | 完整文件 + 切除代码段参考文档 —— **该目录已在 `8aaa4f1` 移出仓库，现不存在** |

**回流说明（2026-10-20）**：放弃独立 DocFix 软件，基础曝光/滤镜并回本项目。依据 `docs/adr/0008-exposure-enhancement.md` + `docs/design/2026-10-20_新功能调研与方案_双面_续扫_纠偏_曝光_v1.0.md` §0 需求 F。曝光/预设代码入库（`267a360`），接入预览 GUI 与落盘链路（`8bde8dc`）。

### 4.4 文档会话功能（2026-10-21 批次 4）

GUI 参数区新增「扫描模式」单选：**单面** / **双面（手动）** / **加入现有文档（续扫）**（`hp_scan_gui_pyside.py:1945-1954`）。后两种走**会话**（`document_session.py`）；单面走既有预览/直存流程、不用会话。

| 功能 | 怎么用 | 限制 / 前提 | 代码落点 |
|------|--------|------------|----------|
| 手动双面扫描 | 选「双面（手动）」→ 扫正面 → 整叠纸**翻面**放回输稿器 → 再点「扫描」扫背面 → 合并预览确认页序 → 落盘 | **只支持输稿器（ADF）**（平板没有背面）；双面 = **两趟单面**（下发给引擎的是 `"duplex": False`，合并由本地算法做）；扫描仪自带的「双面扫描」复选框与本功能无关 | `hp_scan_gui_pyside.py` 的 `_pending_side_by_job` / `_session_batch_finished` / `_ensure_session`；`document_session.py` 的 `PageRef.side`（`SIDES = ("single","front","back")`）；`doc_merge.interleave_duplex` |
| 断点续扫 | 扫描被打断时，在强提醒里点「中断并从第 N 页重扫」⇒ 保留前 N-1 页、丢弃第 N 页及之后，重扫接着往下走 | 前提：**设备仍可用** + **已扫的页还在缓存里**（缓存过期线 = `cache_config.json` 的 `stale_hours`，默认 24 小时；`cleanup_stale` 在 `ScanApp.__init__` 里跑，**早于**启动时的会话保护重登记 ⇒ 隔天续扫可能已丢页）；丢弃范围**只限该批次**，其它批次的页不受影响；**单面模式没有会话** ⇒ 该按钮只中止这次扫描，已扫页留在缓存里、不进会话 | `hp_scan_gui_pyside.resume_from_page` / `ScanApp._resume_from_page` / `_absorb_cancelled_pages`；`document_session.DocumentSession.remove_pages_from` / `resume_scanning` |
| 大批量合并成单个 PDF | 「加入现有文档（续扫）」一批批扫 → 点「结束并保存」→ 合并预览确认页序 → 落盘 | 输出 PDF ⇒ 合成**一个** PDF；jpg/png/tiff ⇒ 每页一个文件（页号跨批次连续）；预计合成内存峰值 > 1.5GB 会先问「仍要继续吗」（不静默截断）；**部分页失败时保留缓存**以便重试 | `save_manager.save_session`（B4-5）；页序规则见下表 |
| 自动纠偏 + 倾斜强提醒 | 每页进缓存后自动测倾斜角；连续超阈值弹强提醒 ⇒ 「继续扫描（自动纠偏）」/「中断并从第 N 页重扫」/「强制保存（不纠偏）」 | 默认阈值：强提醒 3.0°、连续 3 页；施加阈值 0.5°；检出下限 0.75°（`skew_detect._MIN_EFFECTIVE_ANGLE`）、检测范围 ±8°；空白页/墨迹过少不判角度；「强制保存」的页原样保存不纠偏；⚠️ **界面里没有纠偏开关**（4 个键只有默认值） | `skew_detect.estimate_skew` / `deskew_image`；`scan_coordinator.ScanCoordinator._note_skew`（发 `skew_warning` 信号）；`hp_scan_gui_pyside.SkewWarningDialog`（`CHOICE_RESUME` / `CHOICE_CONTINUE` / `CHOICE_FORCE`）；`save_manager._session_page_specs` + `deskew_angle_to_apply` |

**页序规则（`doc_merge`；GUI 预览与落盘走同一套调用）**：

| 场景 | 规则 | 出处 |
|------|------|------|
| 双面 | **先反转、再交错、多余的接末尾**：背面默认整叠翻面 ⇒ 先 `reversed(back)`，再 F1/B末、F2/B末-1… 交错；两边页数不等时，多出的那几页按原序接在末尾。界面可取消勾选「背面顺序反转」 | `doc_merge.interleave_duplex`（`reverse_back=True` 为默认）；会话字段 `DocumentSession.reverse_back` |
| 批次 / 续扫 | **批序 + 批内原序**：按 job_id 分组（首次出现顺序），组内保持原序，再顺序拼接 | `doc_merge.merge_batches`；`save_manager._session_merged_pages` 与 `hp_scan_gui_pyside.session_merged_pages`（同一套规则） |

**会话状态与缓存保护**：`scanning → awaiting_flip`（双面扫完正面）/ `paused`（续扫扫完一批）/ `completed`（落盘成功）/ `abandoned`（用户放弃）。会话文件在 `%APPDATA%\HP_Scan_Tool\sessions\`（`document_session.sessions_dir()`）。会话引用的 job 目录被**显式保护**（不被 eviction / `cleanup_stale` 自动清理），落盘成功或放弃后解除保护并清缓存 + 页元数据。⚠️ 保护集合是**进程内**的 ⇒ 重启后靠 `ScanApp._restore_open_sessions` 重新登记（启动时弹「有未完成的文档会话，是否继续？」）。

> ⚠️ **这四项只走 GUI**：CLI 没有对应子命令（`cli.py` 仍是 scan / discover / status / history 四个），别按 GUI 能力去推断 CLI。

---

## 5. 关键技术决策与经验教训

### 5.1 图标方案（经过大量迭代）

**v4.0 实际方案**：PySide6 `QIcon` + PNG 资源 —— `hp_scan_gui_pyside.py:203` `get_resource_path(os.path.join("icons", "icon_64.png"))`

> ⚠️ **2026-10-21 订正**：本节原先描述的是 **CustomTkinter（v3.1）** 方案 —— `iconphoto(True/False, Npx PNG)`、`SetProcessDpiAwareness(2)`、`SetCurrentProcessExplicitAppUserModelID("HP.ScanTool.v4.0")`。这三个 API 在 v4.0（PySide6）代码里**全部不存在**：全仓 `*.py` 搜 `iconphoto|SetProcessDpiAwareness|SetCurrentProcessExplicitAppUserModelID` 只命中 `hp_scan_gui_pyside.py:4154` 的一句**注释**（"提前设置环境变量可抑制 Qt 内部的 SetProcessDpiAwarenessContext 警告"），不是实现。
> 下半段的**经验教训仍然有效**，但**机制部分一律以代码为准**。

- v4.0：`QIcon(...icons/icon_64.png)`，32px / 64px 由 Qt 从同一张 64px PNG 缩放，**没有** v3.1 的 `iconphoto` 双尺寸调用
- v4.0：**未**显式调用 `SetProcessDpiAwareness(2)`，也**未**调用 `SetCurrentProcessExplicitAppUserModelID` ⇒ 任务栏分组行为依赖 Qt 自身
- AI 生成的图片不适合做图标（细节太多缩小后糊），必须用 ImageDraw 绘制纯几何图形
- PIL 的 ICO save 不支持真正的多尺寸（append_images 对 ICO 无效），需手写 struct 格式

### 5.2 PySide6 布局经验

- `QThread` + Signal 完成所有 IO 操作，UI 更新通过 `QTimer.singleShot(0, callback)`
- 扫描仪列表用 `_scanners_lock` 保护
- 配置写入原子性：`.tmp` + `os.replace`
- 资源路径：开发模式用 `resources/`，EXE 模式用 `sys._MEIPASS`

### 5.3 eSCL 协议经验

- `NextDocument` 轮询比 Job URI 更稳定（M232 等机型 Job URI 返回 404）
- 双协议发现：mDNS 为主，WSD 兜底，按 IP 去重
- HTTPS 301 重定向**已跟随**（http 目标直接跟、https 目标走宽松 TLS 重试：`escl_engine.py:332` 的 `_http_get_loose_tls`）

### 5.4 PyInstaller 打包

- 动态 import 的模块必须加入 hiddenimports
- EXE 被占用时用 `--distpath` 指定不同目录绕过文件锁
- 字体/图标由 `build_exe.py` 的 `--add-data resources;resources` 一并打包（★ onedir 模式必须用**绝对路径**，相对路径会按 spec 目录解析）
- ★ 发行形态有两套：**安装包**（onedir + Inno Setup，`build_installer.py`）与**单文件便携版**（onefile，`build_exe.py`）——
  详细差异与安装器踩坑见 §8.2
- 单文件 EXE **66,003,273 字节（62.95 MiB）** —— **2026-10-21 18:3x 用最终代码重打的实测值**（SHA256 `0D6AF3C68FB003D635D8CB53B90A6E7C1E8BB822D5D231EB82FB122E166C475A`；offscreen 启动 6 秒未崩；`naming` / `escl_engine` 均在包内）。★ **已替换 `dist/HP_Scan_Tool.exe`**（2026-10-21 18:40，回读字节数与 SHA256 与新包逐字节一致）。⚠️ 此前文档里的数字**均已过期**：66,004,536 字节（17:2x 那次）、65,987,523 字节 / 62.93 MiB（11:1x 那次）、65,866,395 字节 / 62.80 MiB（01:30 的包，`19efd5c` 排除 numpy 后重打的）、「63,452,740 字节旧包（2026-07-28）」、「约 62 MB（2026-10-20 改造前基线实测 64,968,184 字节）」
- ★ **安装包** **46,603,297 字节（44.4 MiB）** —— `build/release/HP_Scan_Tool_Setup_v4.0.0.0.exe`
  （SHA256 `2EA8E85AFA2175FDA45375CD704922FEDAE577EFAF9A57CBBA8CAC1D995D73D8`；
  真机验证：静默安装成功（**不需管理员**）→ 跑起来 **TEMP 无 `_MEI`** → 静默卸载 →
  程序目录/卸载条目/快捷方式全清、**用户数据与扫描成品零丢失**）

---

## 6. 沟通记录摘要

### 6.1 用户偏好

- 用户是全技术栈项目 owner，期望 agent 接手后主动做全维度审计并修复
- "全部执行" = 修复所有发现的问题，非紧急项记录 TODO
- 保存文件前弹确认对话框（已完成）→ 后来改为保存后直接打开文件夹，不要弹窗
- 偏好为参考手册增加细粒度索引和错误速查表来降低 Agent 搜索成本
- 偏好大数据磁盘缓存方案（落盘而非内存）

### 6.2 关键决策历程

1. **图标问题**（经历 ~8 轮迭代）：AI 图片 → 手绘 ICO → iconbitmap → WM_SETICON → 最终 iconphoto + PNG 解决　**（⚠️ 这是 v3.1 CustomTkinter 的最终方案；v4.0 改用 PySide6 `QIcon` + PNG 资源，见 §5.1）**
2. **曝光功能归属**：最初考虑在 HP Scan Tool 内加曝光面板 → 用户提议做成独立软件 → 讨论后决定曝光+扶正等功能全部剥离到独立的 DocFix 工具 → **2026-10-20 决定放弃独立 DocFix 软件，曝光/预设并回本项目**（扶正仍未并入）
3. **打印机排序**：用户要求可调整打印机优先级，重启后记住 → 通过 list 重排 + config 持久化实现
4. **架构分离**：用户提出"HP Scan Tool 仅用于扫描" → 剥离所有图像后处理代码到 `_docfix_reserve/`（**2026-10-20 该决策对曝光/预设已回退，见 §4.3**）
5. **UI 框架迁移**：从 CustomTkinter 迁移到 PySide6，解决 CTk 的渲染和兼容性问题

---

## 7. 后续工作

### 7.1 曝光/预设（2026-10-20 起并回本项目）

**目标**: 基础曝光/滤镜在本项目内闭环，放弃独立 DocFix 软件
**代码来源**: 本仓库 `exposure.py` / `preset_manager.py`（原 `_docfix_reserve/` 备份目录已在 `8aaa4f1` 移出仓库，现不存在）
**核心功能**:
- 曝光矫正（去灰底/亮度/对比度/Gamma/阴影高光/通道增益）—— ✅ 已交付（`ExposurePanel` + 落盘链路）
- 预设系统（4 个内置预设 + 用户自定义）—— ✅ 已交付（`%APPDATA%\HP_Scan_Tool\presets.json`）
- 文档扶正/去歪斜 —— ✅ **已交付**（`skew_detect.py`，纯 PIL 零依赖）：**检测与落盘施加纠偏均已接入扫描流水线**（`c6037a4`）—— 每页写入缓存后调 `estimate_skew` 测角度、写进 `page_meta.deskew_angle`（绝不旋转缓存原件），落盘时 `save_manager.save_pages` 按阈值施加纠偏。`skew_detect` 是**静态 import**（`scan_coordinator.py:30` / `save_manager.py:57`）⇒ PyInstaller 依赖分析实测 FOUND ⇒ **随 EXE 交付**。⚠️ **GUI 里没有纠偏开关** —— `deskew_enabled` / `deskew_apply_threshold` / `skew_warn_threshold` / `skew_warn_consecutive` 这 4 个键只有默认值（`save_manager.DESKEW_CONFIG_DEFAULTS`），界面不可调
- 后续可能加：裁边、去污、OCR

**技术要点**:
- `exposure.py` 的 `apply_exposure()` 是统一入口，支持 off/auto/manual 三种模式
- auto 模式用高斯模糊除法（CamScanner/ScanTailor 同源算法），全分辨率 0.4 秒
- manual 模式处理链：channel_gains → shadows/highlights → gamma → brightness/contrast
- `preset_manager.py` 的预设数据结构：`{name, mode, brightness, contrast, gamma, shadows, highlights, channel_gains, builtin}`
- 曝光 UI 的滑块/预设选择等代码已随 2026-10-20 回流由 `ExposurePanel` 重新实现（原 `stripped_code_reference.py` 随 `_docfix_reserve/` 一并移出仓库）

### 7.2 HP Scan Tool 低优先级待办

| 项目 | 说明 |
|------|------|
| macOS 平台抽象层 | `platform_engine.py`，ADR-0012 已记录未实现 |
| `os.startfile` 跨平台适配 | macOS 需用 `open` 命令 |
| CONTEXT.md 更新 | ✅ 已完成（2026-10-20：术语表边界说明改为"曝光/预设已并回本项目"） |
| PRD.md 更新 | ✅ 已完成（2026-10-20：PRD 顶部边界说明与决策 #39 同步订正） |
| exposure.py / preset_manager.py 清理 | ✅ 不适用 —— 两个文件**已在用**（`hp_scan_gui_pyside.py` / `save_manager.py` 引用），不可删除 |

### 7.3 已知限制

| 限制 | 说明 |
|------|------|
| M232 能力查询 HTTPS 301 | 部分机型 HTTPS 端口返回 301 重定向，**已跟随**（http 目标直接跟、https 目标走宽松 TLS 重试：`escl_engine.py:332`） |
| 无扫描预览（原图查看） | 简化后的 PreviewDialog 只加载缩略图，不做全分辨率预览 |
| 单文件 EXE 体积 | **66,003,273 字节（62.95 MiB）**，2026-10-21 18:3x 用最终代码重打实测（SHA256 `0D6AF3C6…C475A`），包含所有依赖；★ 已覆盖 `dist/HP_Scan_Tool.exe`（18:40，回读校验一致）。⚠️ 此前数字 66,004,536 / 65,987,523 / 65,866,395 / 63,452,740 字节与「改造前基线 64,968,184 字节」均已过期 |
| 无自动更新 | 当前版本无自动检查更新机制 |

---

## 8. 构建与运行

### 8.1 开发环境运行

```bash
cd <PROJECT_DIR>
pip install -r requirements.txt
python hp_scan_gui_pyside.py
```

### 8.2 打包（两种发行形态）

```bash
python build_installer.py    # ★ 正式部署：onedir 目录发布 → Inno Setup → 安装包 + 出包自检
python build_exe.py          # 便携版：onefile 单文件
```

| 形态 | 入口 | 产物 | 说明 |
|---|---|---|---|
| **安装包**（★ 发给每个人） | `build_installer.py` | `build/release/HP_Scan_Tool_Setup_v4.0.0.0.exe`（44.4 MiB） | per-user 安装（**不需要管理员**）+ 标准卸载 + 快捷方式 |
| 便携版 | `build_exe.py` | `dist/HP_Scan_Tool.exe`（62.9 MiB 单文件） | 免安装；**每次启动解压 138.5 MiB 到 `%TEMP%`**，崩溃/强杀会残留 |

**★ 为什么正式部署用安装包（onedir）而不是单文件**
onefile 每次启动把 138.5 MiB 解压到 `%TEMP%\_MEIxxxxxx`（实测启动 3.3 秒）⇒
① 别人「清空 `%TEMP%`」会**打断正在运行的程序**（DLL 被抽走）；
② 崩溃/强杀**残留** 138.5 MiB/次（进程死后能删）；
③ 卸载要清 `%TEMP%` 里的东西就绕不开。
onedir 安装后是**真实文件**，**完全不解压 `%TEMP%`**（真机验证：运行期间 `_MEI` 增量为 0）。

**安装器要点**（`installer/hp_scan_tool.iss`，Inno Setup 6.3+）
- `.iss` 是 **UTF-8 无 BOM**；6.3 以下必须加 BOM（否则中文向导乱码）⇒ 脚本内有**版本闸门**
  `#if VER < EncodeVer(6,3,0) #error`，编译期直接报错。
- `MessagesFile` 用**相对 .iss 的路径**（`installer/Languages/ChineseSimplified.isl`，仓库自带）
  ⇒ 换机 / CI 装的是 Inno 6 时不会因绝对路径而失败。
- `AppId` 写法 `{{GUID}`（**两个**左花括号、**一个**右花括号）—— 多一个右花括号会写坏卸载键名。
- 升级覆盖安装：`CloseApplications=force` + `InitializeSetup` 里 `taskkill /F /IM` 兜底强退旧实例。
- `VersionInfoVersion` 必须写（不写 ⇒ Setup 壳的**文件版本**是 0.0.0.0，肉眼极难发现）。
- `[Tasks]` 默认**是勾选**的，取消要写 `Flags: unchecked`（写 `Flags: checked` 会**中止编译**）。
- 卸载清理用户数据用 **`SuppressibleMsgBox(..., IDNO)`** 而不是 `MsgBox`：
  普通 `MsgBox` 在**静默模式**下不显示、直接返回 **IDYES** ⇒ 静默卸载会误删用户数据。
  ★ 静默模式返回默认的 `IDNO` ⇒ **保留数据**。
- ★ 卸载只删 `Documents\HP_Scans\**.cache**`（中间产物）——
  `Documents\HP_Scans\` 的**根目录就是默认输出目录**（`scan_coordinator.DEFAULT_OUT_DIR`）
  ⇒ 用户的**扫描成品**在那里，整目录删会毁掉用户扫出来的文件。

**出包自检**：`python tools/verify_release.py` ⇒ 校验 onedir 完整性 + 无开发期残留 +
安装包合法 PE + 文件名版本号 ⇒ 生成清单 `build/release/MANIFEST.txt`（每件大小与 sha256）。

`build_exe.py` 仍是单文件便携版的入口（`--onefile` 单文件模式）。仓库不再保留独立的 `.spec` 文件——它此前是 onedir 配置，与 CI 实际运行的 onefile 模式冲突，且长期未被使用。

注意：打包前确认 `build_exe.py` 里的 `--add-data` 资源路径与 `--hidden-import` 模块列表与当前代码一致。**当前项目内模块全部是静态 import**（`exposure` / `preset_manager` / `page_meta` / `config_io` / `pypdf` 等已由 PyInstaller 依赖分析器实测自动收录），**只有改成动态 import 的模块才需要补进 hidden-imports** —— 否则会变成打包后运行时 ImportError。
★ onedir 模式的 `--add-data` **必须用绝对路径**：PyInstaller 6.x 里相对路径是相对
  **spec 文件所在目录**解析的，而 onedir 模式把 `--specpath` 指到了 `build/onedir`。

### 8.3 CLI 模式

```bash
python cli.py scan --ip 192.168.1.100 --resolution 300 --format pdf
python cli.py discover --format json
python cli.py status --ip 192.168.1.100
python cli.py history --limit 10
```

---

## 9. 配置文件说明

### scan_config.json

```json
{
  "resolution": 300,
  "color_mode_ui": "彩色",
  "output_format": "jpg",
  "output_dir": "C:\\Users\\...\\Documents\\HP_Scans",
  "source": "平板",
  "saved_ips": [{"ip": "192.168.1.100", "model": "HP M232"}],
  "nicknames": {"192.168.1.100": "三楼HP"},
  "selected_scanner_ip": "192.168.1.100"
}
```

> ⚠️ **2026-10-21 订正**：原示例里带 `"theme": "dark"` —— **v4.0 没有主题机制**，全仓 `*.py` 搜 `theme` 零命中，既无切换控件也无此配置键（该键是 v3.1 遗留，PRD §6.2 已把整行划掉）。已从示例里删除。
>
> 示例里也省略了纠偏相关的 4 个键（`deskew_enabled` / `deskew_apply_threshold` / `skew_warn_threshold` / `skew_warn_consecutive`）—— 它们**会被程序自动补进 `scan_config.json`**（`scan_coordinator.apply_deskew_defaults`），完整键表见 `PRD.md` §6.2。

注意：`exposure_mode`、`brightness`、`contrast` 字段**不在** `scan_config.json` 里。曝光参数（模式/亮度/对比度/gamma/阴影高光/通道增益）由 `ExposurePanel` 在运行时持有，用户预设持久化在 `%APPDATA%\HP_Scan_Tool\presets.json`；旧配置文件中的这些字段会被忽略。

### cache_config.json

```json
{
  "cache_limit_mb": 1024,
  "trigger_ratio": 0.95,
  "cleanup_mb": 150,
  "stale_hours": 24
}
```

---

## 10. 测试

```bash
cd <PROJECT_DIR>
pytest tests/ -v
```

测试覆盖：缓存 / 历史 / CLI / 配置并发 / eSCL 健壮性 / 预设 / 曝光 / 曝光集成 / GUI 一致性 / 监听 / 页元数据 / 保存 / 协调器 / 倾斜检测 / 文档会话 / 双面合并 等，见 `tests/`。

**测试文件数（2026-10-21 11:1x 工作区实测）**：`tests/` 下共 **79 个 `.py`** —— `conftest.py` 1 个 + `test_*.py` **75 个** + 测试辅助模块 3 个（`cli_subprocess_probe.py` / `fake_http_server.py` / `skew_fixtures.py`，不是测试文件）。

> 统计方式：按文件名对 `tests/` 下的 `.py` 归类计数（`Get-ChildItem tests -Filter *.py -File` 后按 `conftest.py` / `test_*.py` / 其余辅助模块三类归类）。
> ⚠️ 此前文档写的「当前 15 个测试文件：`conftest.py` + 14 个 `test_*.py`」**已过期**。
> 统计口径说明：本轮已把 `tests/` 下**所有**新增测试文件提交入库 ⇒ 工作区与 HEAD 一致；只数 HEAD 里的（`git ls-tree -r --name-only HEAD -- tests/`）同为 **79 个 `.py`**（`conftest.py` 1 + `test_*.py` 75 + 辅助模块 3）。后续若再新增测试，以实际目录为准。

---

*本文档由 Agent 于 2026-09-21 更新，反映 v4.0 PySide6 架构。*

---

## 新 Agent 阅读顺序建议

1. **README.md** — 项目入口，快速了解功能与使用
2. **本文档 (HANDOVER.md)** — 全貌概览，架构与经验教训
3. **CODEBUDDY.md** — CodeBuddy 项目指引，包含架构、资源、配置等关键信息
4. **PRD.md** — 产品需求文档，了解产品定位和功能规格（注意：曝光/预设已于 2026-10-20 并回本项目，PRD 顶部边界说明已同步订正）
5. **docs/SPEC-v3.3.md** — v3.3 规格文档，User Stories 和技术方案（历史文档）
6. **docs/tickets.md** — 实施票据和依赖图（注意：T2/T3 关于曝光的内容已于 2026-10-20 并回本项目并接线；T10 CLI 的 `--exposure` 参数**已于 2026-10-21 实现**，取的是**预设名**而不是 MODE）
7. **docs/adr/** — 架构决策记录，了解关键技术选型
8. **CONTEXT.md** — 领域术语表（注意：曝光/曝光预设的边界说明已于 2026-10-20 订正；术语表正文已于 2026-10-21 补上「曝光」「预设」等 6 个词条）
9. **源代码** — 按以下顺序阅读：
   - `scan_coordinator.py` → 业务层入口
   - `escl_engine.py` → 核心协议
   - `hp_scan_gui_pyside.py` → GUI 层
   - `cache_manager.py` → 缓存机制

## 文档时效性说明

| 文档 | 状态 | 说明 |
|------|------|------|
| README.md | ✅ 已同步（2026-10-21） | 功能特性已补批次 4 四项（手动双面 / 断点续扫 / 大批量合并 / 自动纠偏 + 倾斜强提醒）；项目结构已补 `document_session.py` / `doc_merge.py`，配置文件已补 `sessions/` |
| HANDOVER.md | ✅ 最新 | 本文档，反映 v4.0 PySide6 架构 |
| CODEBUDDY.md | ⚠️ 部分过时 | `:112` 仍写"曝光/预设已剥离至 `_docfix_reserve/`、不得在新代码中引用"，与代码相反（2026-10-20 已并回本项目），**待该文件责任人订正** |
| PRD.md | ⚠️ 部分过时 | 曝光/预设边界已订正；§2.4 已补批次 4 四项功能规格（2026-10-21）；结构块与线程模型仍停留在 v3.1 CustomTkinter 时代 |
| CONTEXT.md | ✅ 有效 | 曝光/预设边界说明已订正；术语表正文已于 2026-10-21 补上 曝光 / 预设 / 能力已知 / 命名形态 / 倾斜角与纠偏 / 扫描会话 六个词条 |
| docs/SPEC-v3.3.md | ⚠️ 部分过时 | 关于曝光增强/预设系统的 User Story 为 v3.3 历史记载（现已并回本项目） |
| docs/tickets.md | ⚠️ 部分过时 | T2/T3 已于 2026-10-20 并回本项目并接线；T10 的 `--exposure` **已于 2026-10-21 实现**（取**预设名**；预设不存在 ⇒ 中文报错 + 退出码 2 + 不扫描不落盘） |
| docs/adr/ | ✅ 有效 | 12 份 ADR 均有效，ADR-0012 未实现但不影响当前功能 |
