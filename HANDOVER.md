# HP Scan Tool — 项目交接文档

> 日期: 2026-09-21 | 当前版本: v4.0（PySide6 重写版）

---

## 1. 项目概况

HP Scan Tool 是一款面向惠普网络打印机的 Windows 桌面扫描客户端，通过 eSCL/AirScan 协议实现免驱网络扫描，WIA 作为降级方案。

**项目路径**: `c:\Users\YQQ-Agent\Desktop\PC传输专用\HP_Scan_Tool`
**Git 仓库**: 已初始化（`.git` 存在），remote 指向 `Papachong/HP_Scan_Tool`
**技术栈**: Python 3.11+ / PySide6 / Pillow / python-zeroconf / WSDiscovery / requests / pywin32
**打包**: PyInstaller 单文件 EXE（spec 文件已配置）

### 1.1 当前状态（2026-09-21 v4.0）

HP Scan Tool 已稳定为**纯扫描工具**，职责边界清晰：

- 发现设备（mDNS + WSD 双协议）
- 选择扫描仪、配置参数（分辨率/颜色/格式/来源）
- 执行扫描（平板单页 / ADF 多页）
- 缓存 → 预览 → 保存原始文件
- 记录扫描历史

**不再包含任何图像后处理逻辑**（曝光矫正、预设、直方图等已全部剥离）。

---

## 2. 文件结构

```
c:\Users\YQQ-Agent\Desktop\PC传输专用\HP_Scan_Tool\
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
├── build_exe.py            # PyInstaller 打包脚本
├── HP_Scan_Tool.spec       # PyInstaller spec 文件
├── app.ico                 # 多尺寸图标（16-256px，手写 ICO）
├── icon_16.png             # 任务栏图标 16px（代码绘制几何图形）
├── icon_32.png             # 任务栏图标 32px（代码绘制几何图形）
├── icon_64.png             # 任务栏图标 64px（代码绘制几何图形）
├── fonts/
│   └── NotoSansSC-Regular.ttf
├── resources/
│   ├── fonts/
│   ├── styles/
│   │   └── app.qss
│   └── icons/
├── tests/                  # pytest 测试
│   ├── conftest.py
│   ├── test_cache_manager.py
│   ├── test_cli.py
│   └── test_history_manager.py
├── docs/                     # 项目文档
│   ├── SPEC-v3.3.md              # v3.3 规格文档（User Stories + 技术方案）
│   ├── tickets.md                # 实施票据 T1-T10（含依赖图）
│   ├── adr/                      # 架构决策记录（12 份 ADR）
│   │   ├── 0001 ~ 0011           # 格式映射/多页/缓存/WSD/CLI 等
│   │   └── 0012-platform-abstraction-runtime.md  # 平台抽象层（未实现）
│   ├── prototype/
│   │   └── multipage-preview.jsx # 多页预览 UI 原型（React）
│   └── research/
│       └── wsd-discovery.md      # WSD 发现协议调研
├── _docfix_reserve/        # ★ 剥离代码备份（供 DocFix 项目使用）
│   ├── exposure.py             # 完整图像曝光引擎
│   ├── preset_manager.py       # 完整预设管理器
│   ├── test_preset_manager.py  # 预设管理测试
│   └── stripped_code_reference.py  # 从各文件切除的代码段汇总
├── PRD.md                  # 产品需求文档 v3.1
├── CONTEXT.md              # 领域术语表
├── CODEBUDDY.md            # CodeBuddy 项目指引
├── requirements.txt        # Python 依赖
├── run.bat                 # 开发模式启动脚本
├── backup.bat              # 下班存档脚本
├── .gitignore              # Git 忽略规则
├── scan_config.json        # 运行时配置（自动生成）
├── cache_config.json       # 缓存配置（自动生成）
└── history.json            # 扫描历史（自动生成）
```

---

## 3. 架构概览

### 3.1 当前架构（v4.0 PySide6）

```
hp_scan_gui_pyside.py (GUI 层 — PySide6)
  ├── ScanApp                  — 主窗口：扫描仪管理 + 参数配置 + 扫描启动
  ├── LoadingDialog            — 启动进度对话框
  ├── ScanWorker               — 单页扫描 QThread
  ├── MultiPageScanWorker      — 多页扫描 QThread
  ├── ScannerCard              — 扫描仪卡片组件（选中/重命名/禁用）
  ├── ImageViewer              — 图片预览（缩放/旋转）
  ├── AnimatedButton           — 按压动画按钮
  ├── PreviewDialog            — 单页预览 + 文件名确认 + 保存
  ├── MultiPagePreviewDialog   — 多页预览：缩略图 + 分组 + 批量保存
  └── HistoryDialog            — 扫描历史浏览器

scan_coordinator.py (业务层)
  └── ScanCoordinator          — 扫描仪发现/探活/扫描执行/缓存协调

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

### 3.3 已剥离的后处理架构（迁移至 DocFix）

```
exposure.py (已停用)
  ├── _auto_enhance()           — 高斯模糊除法文档增强（去灰底）
  ├── apply_exposure()          — 统一曝光入口（off/auto/manual）
  ├── _gamma_lut()              — Gamma 校正 LUT
  ├── _shadow_highlight_lut()   — 阴影/高光分离 LUT
  └── auto/manual_exposure()    — bytes 向后兼容接口

preset_manager.py (已停用)
  ├── _BUILTIN_PRESETS          — 4 个内置预设（文字增强/照片还原/去灰底/旧文件修复）
  ├── load_presets()            — 加载内置+用户预设
  ├── save/delete_user_preset() — 用户预设管理
  └── export/import_preset()    — 预设导入导出
```

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

### 4.2 P1 体验优化（全部完成）

| 功能 | 状态 | 说明 |
|------|------|------|
| 深色/浅色主题 | ✅ | 一键切换，配置持久化 |
| 打印机自定义名称 | ✅ | 按 IP 关联，配置持久化 |
| 打印机优先级排序 | ✅ | ▲▼ 按钮调整顺序，重启后记住 |
| 扫描仪选中持久化 | ✅ | 按 IP 恢复上次选中的扫描仪 |
| 任务栏图标清晰 | ✅ | iconphoto + PNG + SetProcessDpiAwareness(2) |
| 任务栏图标不闪烁 | ✅ | SetCurrentProcessExplicitAppUserModelID |
| 窗口防逐帧渲染 | ✅ | withdraw → build → update_idletasks → deiconify |
| 嵌入式字体 | ✅ | AddFontResourceExW 私有加载，ClearType 优化 |
| 多页分组管理 | ✅ | 插入分割/移除分割/重命名分组 |
| 页面删除 | ✅ | 多页预览中删除不需要的页面 |
| 并发扫描 | ✅ | 多台打印机同时扫描，独立缓存 |
| ADF 自动检测 | ✅ | 扫描前查询 adf_loaded，自动切换平板/输稿器 |

### 4.3 架构分离（2026-07-19 完成）

| 操作 | 详情 |
|------|------|
| 剥离 exposure.py | 从各模块中移除所有曝光调用 |
| 剥离 preset_manager.py | 不再被任何活跃模块导入 |
| 简化 PreviewDialog | 去掉曝光调整/直方图/预设，只保留预览+保存 |
| 简化 MultiPagePreviewDialog | 去掉曝光控制/逐页覆盖，保留缩略图/分组/保存 |
| 清理 spec 文件 | 移除 exposure/preset_manager/ImageFilter/ImageMath |
| 清理测试 | 移除 conftest.py 和 test_cli.py 中的曝光相关内容 |
| 备份到 _docfix_reserve/ | 完整文件 + 切除代码段参考文档 |

---

## 5. 关键技术决策与经验教训

### 5.1 图标方案（经过大量迭代）

**最终方案**: `iconphoto` + PNG 文件（比 `iconbitmap` + ICO 清晰得多）

- `iconphoto(True, 64px PNG)` — True 表示应用到所有 Toplevel 窗口
- `iconphoto(False, 32px PNG)` — 小尺寸用 32px
- 必须调用 `SetProcessDpiAwareness(2)` 防止 Windows 位图拉伸
- 必须调用 `SetCurrentProcessExplicitAppUserModelID("HP.ScanTool.v4.0")` 统一任务栏身份
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
- HTTPS 301 重定向暂不跟随（M232 等机型）

### 5.4 PyInstaller 打包

- 动态 import 的模块必须加入 hiddenimports
- EXE 被占用时用 `--distpath` 指定不同目录绕过文件锁
- spec 的 datas 打包字体/图标文件
- 单文件 EXE 约 22MB

---

## 6. 沟通记录摘要

### 6.1 用户偏好

- 用户是全技术栈项目 owner，期望 agent 接手后主动做全维度审计并修复
- "全部执行" = 修复所有发现的问题，非紧急项记录 TODO
- 保存文件前弹确认对话框（已完成）→ 后来改为保存后直接打开文件夹，不要弹窗
- 偏好为参考手册增加细粒度索引和错误速查表来降低 Agent 搜索成本
- 偏好大数据磁盘缓存方案（落盘而非内存）

### 6.2 关键决策历程

1. **图标问题**（经历 ~8 轮迭代）：AI 图片 → 手绘 ICO → iconbitmap → WM_SETICON → 最终 iconphoto + PNG 解决
2. **曝光功能归属**：最初考虑在 HP Scan Tool 内加曝光面板 → 用户提议做成独立软件 → 讨论后决定曝光+扶正等功能全部剥离到独立的 DocFix 工具
3. **打印机排序**：用户要求可调整打印机优先级，重启后记住 → 通过 list 重排 + config 持久化实现
4. **架构分离**：用户提出"HP Scan Tool 仅用于扫描" → 剥离所有图像后处理代码到 `_docfix_reserve/`
5. **UI 框架迁移**：从 CustomTkinter 迁移到 PySide6，解决 CTk 的渲染和兼容性问题

---

## 7. 后续工作

### 7.1 DocFix 项目（计划中）

**目标**: 独立的文档图像处理工具
**代码来源**: `_docfix_reserve/` 目录包含完整的剥离代码和参考文档
**核心功能**:
- 曝光矫正（去灰底/亮度/对比度/Gamma/阴影高光/通道增益）
- 预设系统（4 个内置预设 + 用户自定义）
- 文档扶正/去歪斜（待实现，推荐 `deskew` 库，522 stars）
- 后续可能加：裁边、去污、OCR

**技术要点**:
- `exposure.py` 的 `apply_exposure()` 是统一入口，支持 off/auto/manual 三种模式
- auto 模式用高斯模糊除法（CamScanner/ScanTailor 同源算法），全分辨率 0.4 秒
- manual 模式处理链：channel_gains → shadows/highlights → gamma → brightness/contrast
- `preset_manager.py` 的预设数据结构：`{name, mode, brightness, contrast, gamma, shadows, highlights, channel_gains}`
- `stripped_code_reference.py` 包含从 hp_scan_gui.py 中切除的 UI 代码（滑块/直方图/预设选择/逐页覆盖等）

### 7.2 HP Scan Tool 低优先级待办

| 项目 | 说明 |
|------|------|
| macOS 平台抽象层 | `platform_engine.py`，ADR-0012 已记录未实现 |
| `os.startfile` 跨平台适配 | macOS 需用 `open` 命令 |
| CONTEXT.md 更新 | 术语表中"曝光"和"预览"的定义需要更新，反映已剥离的事实 |
| PRD.md 更新 | 需求文档中关于曝光预设的描述需要标注为"已迁移至 DocFix" |
| exposure.py / preset_manager.py 清理 | 项目根目录的已停用文件是否删除（目前保留供参考） |

### 7.3 已知限制

| 限制 | 说明 |
|------|------|
| M232 能力查询 HTTPS 301 | 部分机型 HTTPS 端口返回 301 重定向，当前未跟随 |
| 无扫描预览（原图查看） | 简化后的 PreviewDialog 只加载缩略图，不做全分辨率预览 |
| 单文件 EXE 体积 | 约 22MB，包含所有依赖 |
| 无自动更新 | 当前版本无自动检查更新机制 |

---

## 8. 构建与运行

### 8.1 开发环境运行

```bash
cd c:\Users\YQQ-Agent\Desktop\PC传输专用\HP_Scan_Tool
pip install -r requirements.txt
python hp_scan_gui_pyside.py
```

### 8.2 PyInstaller 打包

```bash
python build_exe.py
# 或手动:
pyinstaller HP_Scan_Tool.spec --distpath dist9
```

输出：`dist/HP_Scan_Tool.exe`（单文件便携版）

注意：打包前确认 spec 文件的 datas 和 hiddenimports 与当前代码一致。

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
  "theme": "dark",
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

注意：`exposure_mode`、`brightness`、`contrast` 字段已从配置中移除。旧配置文件中的这些字段会被忽略。

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
cd c:\Users\YQQ-Agent\Desktop\PC传输专用\HP_Scan_Tool
pytest tests/ -v
```

测试覆盖：cache_manager、history_manager、cli 参数解析。
注意：test_preset_manager.py 已移至 `_docfix_reserve/`，不在测试范围内。

---

*本文档由 Agent 于 2026-09-21 更新，反映 v4.0 PySide6 架构。*

---

## 新 Agent 阅读顺序建议

1. **README.md** — 项目入口，快速了解功能与使用
2. **本文档 (HANDOVER.md)** — 全貌概览，架构与经验教训
3. **CODEBUDDY.md** — CodeBuddy 项目指引，包含架构、资源、配置等关键信息
4. **PRD.md** — 产品需求文档，了解产品定位和功能规格（注意：其中关于曝光预设的描述已迁移至 DocFix）
5. **docs/SPEC-v3.3.md** — v3.3 规格文档，User Stories 和技术方案（历史文档）
6. **docs/tickets.md** — 实施票据和依赖图（注意：T2/T3 关于曝光的内容已剥离，T10 CLI 的 `--exposure` 已移除）
7. **docs/adr/** — 架构决策记录，了解关键技术选型
8. **CONTEXT.md** — 领域术语表（注意：部分术语如"曝光""预览"定义需更新）
9. **源代码** — 按以下顺序阅读：
   - `scan_coordinator.py` → 业务层入口
   - `escl_engine.py` → 核心协议
   - `hp_scan_gui_pyside.py` → GUI 层
   - `cache_manager.py` → 缓存机制

## 文档时效性说明

| 文档 | 状态 | 说明 |
|------|------|------|
| README.md | ✅ 最新 | 项目入口文档 |
| HANDOVER.md | ✅ 最新 | 本文档，反映 v4.0 PySide6 架构 |
| CODEBUDDY.md | ✅ 最新 | CodeBuddy 项目指引 |
| PRD.md | ⚠️ 部分过时 | 曝光预设相关章节已迁移，其余有效 |
| CONTEXT.md | ⚠️ 部分过时 | "曝光""预览""曝光预设"术语定义需更新 |
| docs/SPEC-v3.3.md | ⚠️ 部分过时 | 关于曝光增强/预设系统的 User Story 已迁移 |
| docs/tickets.md | ⚠️ 部分过时 | T2/T3 已剥离，T10 的 --exposure 已移除 |
| docs/adr/ | ✅ 有效 | 12 份 ADR 均有效，ADR-0012 未实现但不影响当前功能 |
