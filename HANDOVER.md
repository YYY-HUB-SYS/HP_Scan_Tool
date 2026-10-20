# HP Scan Tool — 项目交接文档

> 日期: 2026-09-21 | 当前版本: v4.0（PySide6 重写版）

---

## 1. 项目概况

HP Scan Tool 是一款面向惠普网络打印机的 Windows 桌面扫描客户端，通过 eSCL/AirScan 协议实现免驱网络扫描，WIA 作为降级方案。

**项目路径**: `<PROJECT_DIR>`
**Git 仓库**: 已初始化（`.git` 存在），remote 指向 <GIT_REPO>
**技术栈**: Python 3.11+ / PySide6 / Pillow / python-zeroconf / WSDiscovery / requests / pywin32
**打包**: PyInstaller 单文件 EXE（由 `build_exe.py` 统一构建，无独立 spec 文件）

### 1.1 当前状态（2026-09-21 v4.0）

HP Scan Tool 已稳定为**纯扫描工具**，职责边界清晰：

- 发现设备（mDNS + WSD 双协议）
- 选择扫描仪、配置参数（分辨率/颜色/格式/来源）
- 执行扫描（平板单页 / ADF 多页）
- 缓存 → 预览 → 保存原始文件
- 记录扫描历史

**2026-10-20 起曝光/预设已并回本项目**（放弃独立 DocFix 软件，基础曝光/滤镜在本项目内闭环）：`exposure.py` / `preset_manager.py` 已入库，`ExposurePanel` 已接入两个预览对话框，曝光已接进落盘链路（`save_manager.py`），预设持久化于 `%APPDATA%\HP_Scan_Tool\presets.json`。直方图未实现、未纳入范围；扶正（自动纠偏）引擎**已完成并已接入扫描流水线**（`c6037a4`）：每页写入缓存后调 `skew_detect.estimate_skew` 测倾斜角并写入 `page_meta.deskew_angle`，落盘时按阈值施加纠偏。`skew_detect` 被 `scan_coordinator.py` / `save_manager.py` **静态 import** ⇒ PyInstaller 依赖分析实测 `skew_detect` FOUND ⇒ **随 EXE 交付**。⚠️ 但 **GUI 里没有纠偏开关**（`deskew_enabled` 等 4 个键只有默认值，界面不可改）。

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
├── tests/                  # pytest 测试（15 个文件：conftest.py + 14 个 test_*.py）
│   ├── conftest.py
│   ├── test_cache_manager.py
│   ├── test_cli.py
│   ├── test_config_concurrency.py
│   ├── test_escl_robustness.py
│   ├── test_exposure.py
│   ├── test_exposure_integration.py
│   ├── test_gui_consistency.py
│   ├── test_history_manager.py
│   ├── test_listen_engine.py
│   ├── test_page_meta.py
│   ├── test_preset_manager.py
│   ├── test_save_manager.py
│   ├── test_scan_coordinator.py
│   └── test_skew_detect.py
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
│   └── verify_scan.py      # 真机基准脚本（D1/D2）
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
| 文件名确认对话框 | ✅ | 支持改文件名和路径 |
| 保存后打开文件夹 | ✅ | os.startfile 直接打开，无弹窗确认 |
| 扫描历史 | ✅ | JSON 日志，GUI 历史对话框 |
| CLI 模式 | ✅ | scan/discover/status/history 四个子命令 |
| WIA 降级 | ✅ | eSCL 不可用时自动降级到 WIA |
| 监听模式 | ✅ | 等待打印机面板触发扫描 |

### 4.2 P1 体验优化（除标注为 v3.1 遗留的条目外，均已完成）

| 功能 | 状态 | 说明 |
|------|------|------|
| 深色/浅色主题 | ⚠️ v3.1 遗留 | **v4.0 无主题机制** —— 全仓 `*.py` 搜 `theme` 零命中，GUI 是单主题（`hp_scan_gui_pyside.py:89 COLORS` + `resources/styles/app.qss`），既无切换控件也无 `theme` 配置键。PRD §2.6.2 已把整节标为 v3.1 遗留 |
| 打印机自定义名称 | ✅ | 按 IP 关联，配置持久化 |
| 打印机优先级排序 | ✅ | ▲▼ 按钮调整顺序，重启后记住 |
| 扫描仪选中持久化 | ✅ | 按 IP 恢复上次选中的扫描仪 |
| 任务栏图标清晰 | ✅ | PySide6 实际走 `QIcon`：`get_resource_path("icons/icon_64.png")`（`hp_scan_gui_pyside.py:1101`）。⚠️ `iconphoto` 是 CustomTkinter 的 API，v4.0 已不存在 |
| 任务栏图标不闪烁 | ⚠️ v3.1 遗留 | v4.0 **没有** `SetCurrentProcessExplicitAppUserModelID` 调用（全仓零命中），未做显式任务栏分组设置 |
| 窗口防逐帧渲染 | ✅ | withdraw → build → update_idletasks → deiconify |
| 嵌入式字体 | ✅ | AddFontResourceExW 私有加载，ClearType 优化 |
| 多页分组管理 | ✅ | 插入分割 / 移除分割（`hp_scan_gui_pyside.py:3525`「＋分组线 / －分组线」按钮）。⚠️ **「重命名分组」未实现** —— 落盘文件名由 `save_manager.build_output_name`（`save_manager.py:360`）按组号自动生成（`{base}_{N:03d}.pdf`），用户无法自定义组名 |
| 页面删除 | ✅ | 多页预览中删除不需要的页面 |
| 并发扫描 | ⚠️ 后端支持 / GUI 未开放 | 后端 `scan_coordinator` 用 `QThreadPool` 且 `setMaxThreadCount(4)`（`scan_coordinator.py:305-306`）；但 GUI 扫描期间 `scan_btn.setEnabled(False)`（`hp_scan_gui_pyside.py:1851`）、只跟踪单个 `_current_job_id` ⇒ **GUI 上一次只能扫一台**，与 `PRD.md` §13「单线程扫描，不支持并发」一致 |
| ADF 自动检测 | ✅ | 扫描前查询 adf_loaded，自动切换平板/输稿器 |

### 4.3 架构分离（2026-07-19 完成，**2026-10-20 已回退**）

| 操作 | 详情 |
|------|------|
| 剥离 exposure.py | 从各模块中移除所有曝光调用 —— **2026-10-20 已回流**（见下） |
| 剥离 preset_manager.py | 不再被任何活跃模块导入 —— **2026-10-20 已回流** |
| 简化 PreviewDialog | 去掉曝光调整/直方图/预设，只保留预览+保存 —— **2026-10-20 已恢复曝光面板（`ExposurePanel`）** |
| 简化 MultiPagePreviewDialog | 去掉曝光控制/逐页覆盖，保留缩略图/分组/保存 —— **2026-10-20 已恢复曝光面板** |
| 清理 spec 文件 | 移除 exposure/preset_manager/ImageFilter/ImageMath —— **2026-10-20 已回流**（`exposure.py` 用到 `ImageFilter` / `ImageMath`） |
| 清理测试 | 移除 conftest.py 和 test_cli.py 中的曝光相关内容 —— **2026-10-20 已补回**（`tests/test_exposure.py` / `test_exposure_integration.py` / `test_preset_manager.py`） |
| 备份到 _docfix_reserve/ | 完整文件 + 切除代码段参考文档 —— **该目录已在 `8aaa4f1` 移出仓库，现不存在** |

**回流说明（2026-10-20）**：放弃独立 DocFix 软件，基础曝光/滤镜并回本项目。依据 `docs/adr/0008-exposure-enhancement.md` + `docs/design/2026-10-20_新功能调研与方案_双面_续扫_纠偏_曝光_v1.0.md` §0 需求 F。曝光/预设代码入库（`267a360`），接入预览 GUI 与落盘链路（`8bde8dc`）。

---

## 5. 关键技术决策与经验教训

### 5.1 图标方案（经过大量迭代）

**v4.0 实际方案**：PySide6 `QIcon` + PNG 资源 —— `hp_scan_gui_pyside.py:1101` `get_resource_path(os.path.join("icons", "icon_64.png"))`

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
- HTTPS 301 重定向**已跟随**（http 目标直接跟、https 目标走宽松 TLS 重试：`escl_engine.py:330-343` 的 `_http_get_loose_tls`）

### 5.4 PyInstaller 打包

- 动态 import 的模块必须加入 hiddenimports
- EXE 被占用时用 `--distpath` 指定不同目录绕过文件锁
- 字体/图标由 `build_exe.py` 的 `--add-data resources;resources` 一并打包
- 单文件 EXE **65,866,395 字节（约 62.8 MB）** —— `dist/HP_Scan_Tool.exe` 的**实测值**（LastWriteTime `2026-10-21 01:30`，即 `19efd5c`「排除 Pillow 顺带拖入的 numpy + 补 EXE 图标与版本资源」之后重新打出的包）。⚠️ 此前文档里的两个数字**均已过期**：①「63,452,740 字节旧包（2026-07-28）」—— 仓库 `dist/` 现存的就是 01:30 重新打出的包，不是 7 月旧包；②「约 62 MB（2026-10-20 改造前基线实测 64,968,184 字节）」—— 改造前基线，与当前包无关

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
- 文档扶正/去歪斜 —— ✅ **已交付**（`skew_detect.py`，纯 PIL 零依赖）：**检测与落盘施加纠偏均已接入扫描流水线**（`c6037a4`）—— 每页写入缓存后调 `estimate_skew` 测角度、写进 `page_meta.deskew_angle`（绝不旋转缓存原件），落盘时 `save_manager.save_pages` 按阈值施加纠偏。`skew_detect` 是**静态 import**（`scan_coordinator.py:29` / `save_manager.py:42`）⇒ PyInstaller 依赖分析实测 FOUND ⇒ **随 EXE 交付**。⚠️ **GUI 里没有纠偏开关** —— `deskew_enabled` / `deskew_apply_threshold` / `skew_warn_threshold` / `skew_warn_consecutive` 这 4 个键只有默认值（`save_manager.DESKEW_CONFIG_DEFAULTS`），界面不可调
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
| M232 能力查询 HTTPS 301 | 部分机型 HTTPS 端口返回 301 重定向，**已跟随**（http 目标直接跟、https 目标走宽松 TLS 重试：`escl_engine.py:330-343`） |
| 无扫描预览（原图查看） | 简化后的 PreviewDialog 只加载缩略图，不做全分辨率预览 |
| 单文件 EXE 体积 | **65,866,395 字节（约 62.8 MB）**，实测值（`dist/HP_Scan_Tool.exe`，LastWriteTime `2026-10-21 01:30`，`19efd5c` 排除 numpy 之后重新打出的包），包含所有依赖。⚠️ 旧的「63,452,740 字节旧包（2026-07-28）」与「改造前基线 64,968,184 字节」均已过期 |
| 无自动更新 | 当前版本无自动检查更新机制 |

---

## 8. 构建与运行

### 8.1 开发环境运行

```bash
cd <PROJECT_DIR>
pip install -r requirements.txt
python hp_scan_gui_pyside.py
```

### 8.2 PyInstaller 打包

```bash
python build_exe.py
```

`build_exe.py` 是唯一的打包入口（`--onefile` 单文件模式）。仓库不再保留独立的 `.spec` 文件——它此前是 onedir 配置，与 CI 实际运行的 onefile 模式冲突，且长期未被使用。

输出：`dist/HP_Scan_Tool.exe`（单文件便携版）

注意：打包前确认 `build_exe.py` 里的 `--add-data` 资源路径与 `--hidden-import` 模块列表与当前代码一致。**当前项目内模块全部是静态 import**（`exposure` / `preset_manager` / `page_meta` / `config_io` / `pypdf` 等已由 PyInstaller 依赖分析器实测自动收录），**只有改成动态 import 的模块才需要补进 hidden-imports** —— 否则会变成打包后运行时 ImportError。

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

测试覆盖：缓存 / 历史 / CLI / 配置并发 / eSCL 健壮性 / 预设 / 曝光 / 曝光集成 / GUI 一致性 / 监听 / 页元数据 / 保存 / 协调器 / 倾斜检测 等，见 `tests/`（当前 15 个测试文件：`conftest.py` + 14 个 `test_*.py`）。

---

*本文档由 Agent 于 2026-09-21 更新，反映 v4.0 PySide6 架构。*

---

## 新 Agent 阅读顺序建议

1. **README.md** — 项目入口，快速了解功能与使用
2. **本文档 (HANDOVER.md)** — 全貌概览，架构与经验教训
3. **CODEBUDDY.md** — CodeBuddy 项目指引，包含架构、资源、配置等关键信息
4. **PRD.md** — 产品需求文档，了解产品定位和功能规格（注意：曝光/预设已于 2026-10-20 并回本项目，PRD 顶部边界说明已同步订正）
5. **docs/SPEC-v3.3.md** — v3.3 规格文档，User Stories 和技术方案（历史文档）
6. **docs/tickets.md** — 实施票据和依赖图（注意：T2/T3 关于曝光的内容已于 2026-10-20 并回本项目并接线；T10 CLI 的 `--exposure` 参数仍未实现）
7. **docs/adr/** — 架构决策记录，了解关键技术选型
8. **CONTEXT.md** — 领域术语表（注意：曝光/曝光预设的边界说明已于 2026-10-20 订正，术语表正文尚未补这两个词条）
9. **源代码** — 按以下顺序阅读：
   - `scan_coordinator.py` → 业务层入口
   - `escl_engine.py` → 核心协议
   - `hp_scan_gui_pyside.py` → GUI 层
   - `cache_manager.py` → 缓存机制

## 文档时效性说明

| 文档 | 状态 | 说明 |
|------|------|------|
| README.md | ⚠️ 部分过时 | 项目结构/配置文件/技术栈三块未含本轮新增模块（`save_manager` / `exposure` / `preset_manager` / `skew_detect` / `page_meta` / `config_io` / `presets.json` / `pypdf`），本轮未纳入范围 |
| HANDOVER.md | ✅ 最新 | 本文档，反映 v4.0 PySide6 架构 |
| CODEBUDDY.md | ⚠️ 部分过时 | `:112` 仍写"曝光/预设已剥离至 `_docfix_reserve/`、不得在新代码中引用"，与代码相反（2026-10-20 已并回本项目），**待该文件责任人订正** |
| PRD.md | ⚠️ 部分过时 | 曝光/预设边界已订正；结构块与线程模型仍停留在 v3.1 CustomTkinter 时代 |
| CONTEXT.md | ⚠️ 部分过时 | 曝光/预设边界说明已订正；术语表正文尚未补 曝光/曝光预设 词条 |
| docs/SPEC-v3.3.md | ⚠️ 部分过时 | 关于曝光增强/预设系统的 User Story 为 v3.3 历史记载（现已并回本项目） |
| docs/tickets.md | ⚠️ 部分过时 | T2/T3 已于 2026-10-20 并回本项目并接线；T10 的 --exposure 未实现 |
| docs/adr/ | ✅ 有效 | 12 份 ADR 均有效，ADR-0012 未实现但不影响当前功能 |
