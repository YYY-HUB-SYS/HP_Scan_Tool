# HP Scan Tool v3.3 — 规格文档

## Problem Statement

HP Scan Tool v3.2 作为单页扫描工具运行良好，但面临以下核心限制：

1. **无法处理多页文档** — ADF 输稿器只扫一页就结束，用户需要手动逐页扫描再合并
2. **内存瓶颈阻止并发** — 扫描数据全量驻留内存（单页 300 DPI 彩色扫描约 30-50MB），无法同时操作多台打印机
3. **曝光控制不够精细** — 只有亮度/对比度两个维度，无法处理发黄旧文件、偏色文档等常见场景，也没有直方图辅助
4. **发现协议不完整** — 仅靠 mDNS 发现，部分只注册 WSD 的打印机无法被检测到
5. **缺乏自动化入口** — 没有命令行接口，无法集成到脚本或批处理流程
6. **仅限 Windows** — WIA 降级方案依赖 pywin32 COM，macOS 用户完全无法使用

## Solution

v3.3 通过以下改造解决上述问题：

- 引入磁盘缓存架构，扫描数据直接落盘，内存只保留预览缩略图
- 基于磁盘缓存实现真正并发扫描（多台打印机同时工作）
- 新增多页 ADF 扫描，支持批量预览和自定义页面分组保存
- 增强曝光控制：直方图显示 + Gamma/阴影高光/RGB 通道增益 + 命名预设系统
- 新增 WSD 发现协议，与 mDNS 并行运行
- 新增 CLI 命令行模式（argparse 子命令）
- 新增平台抽象层，支持 macOS（ImageCapture/SANE 后端）
- 配置文件按职责拆分为 4 个独立文件
- 新增轻量级扫描历史

## User Stories

### 多页 ADF 扫描
1. As a user, I want to scan multiple pages from the ADF in one operation, so that I don't have to manually scan each page
2. As a user, I want to preview all scanned pages in a batch view with page navigation, so that I can review the entire document before saving
3. As a user, I want to delete unwanted pages from the batch preview, so that I only save the pages I need
4. As a user, I want to adjust exposure for all pages at once, so that I don't have to adjust each page individually
5. As a user, I want to adjust exposure per-page in batch mode, so that I can fix pages with different lighting conditions
6. As a user, I want PDF output to merge all pages into a single file by default, so that the document stays together
7. As a user, I want to define page groups with split markers, so that a 20-page scan can become 3 separate PDFs (pages 1-5, 6-18, 19-20)
8. As a user, I want to name each page group, so that the output files have meaningful names
9. As a user, I want non-PDF formats to save as individual numbered files, so that each page is independently accessible

### 磁盘缓存与并发
10. As a user, I want to scan from multiple printers simultaneously, so that I don't waste time waiting in queue
11. As a user, I want scan data written to disk cache instead of memory, so that concurrent scans don't exhaust RAM
12. As a user, I want the cache to auto-clean when approaching 95% of the limit, so that I never run out of space mid-scan
13. As a user, I want to configure the cache size limit (default 1024MB), so that I can adjust it to my disk capacity
14. As a user, I want stale cache from crashes to be cleaned on startup, so that disk space isn't wasted

### 曝光增强
15. As a user, I want to see a real-time histogram of pixel distribution, so that I can make informed exposure adjustments instead of guessing
16. As a user, I want gamma correction control, so that I can fix non-linear brightness issues
17. As a user, I want shadow and highlight separate controls, so that I can adjust dark and bright areas independently
18. As a user, I want per-channel (R/G/B) gain adjustment, so that I can fix color casts (e.g., yellowed documents)
19. As a user, I want built-in exposure presets (Text Enhancement, Photo Restore, De-gray, Old Document Fix), so that common scenarios are one-click
20. As a user, I want to save my own named presets, so that I can quickly apply my preferred settings for specific document types
21. As a user, I want to share preset files, so that my team uses consistent scan settings

### 扫描历史
22. As a user, I want a history list of past scans, so that I can find files I scanned previously
23. As a user, I want each history entry to show timestamp, device, page count, and file path, so that I can identify the scan I'm looking for
24. As a user, I want to click a history entry to open the file or its folder, so that I can access it quickly

### WSD 发现
25. As a user, I want the app to discover printers via both mDNS and WSD, so that I can find all compatible devices on the network
26. As a user, I want duplicate discoveries (same IP) to be automatically merged, so that I don't see the same printer twice

### 命令行模式
27. As a system administrator, I want to trigger scans from the command line, so that I can integrate scanning into automated workflows
28. As a system administrator, I want CLI subcommands for discover, status, scan, and history, so that I can script all scanner operations
29. As a system administrator, I want CLI output in machine-readable format (JSON), so that I can pipe results to other tools

### macOS 支持
30. As a macOS user, I want to use the scanning tool on my Mac, so that I can scan from HP printers without Windows

### 配置拆分
31. As a user, I want settings, presets, history, and cache config in separate files, so that each file has a clear purpose and doesn't grow too large
32. As a power user, I want to manually edit or backup preset files, so that I can manage my exposure presets outside the app

## Implementation Decisions

### 架构层

**磁盘缓存架构**
- 扫描数据直接写入 `{用户文档目录}/HP_Scans/.cache/{job_id}/` 目录
- 每个扫描任务独立子目录，互不干扰
- 预览从缓存文件加载 → apply_exposure → thumbnail → 显示（内存中只有缩略图级别数据）
- 确认保存 → 从缓存加载原图 → apply_exposure（最终参数）→ 写入目标 → 清除缓存
- 取消 → 直接清除缓存目录

**缓存清理策略**
- 上限可配置，默认 1024MB（单位 MB）
- 强制清理触发线：上限的 95%（默认 972.8MB）
- 每次自动清除 150MB，FIFO 顺序（最旧优先）
- 写入前检查，接近触发线即清理
- 启动时清理超过 24 小时的残留缓存

**并发扫描**
- 支持多台打印机同时扫描（真正并发）
- 每个扫描任务在独立线程中运行，独立缓存子目录
- 每个任务完成后独立弹出预览

**多页 ADF 扫描**
- 新建 execute_multipage_scan 函数，不修改现有 execute_scan
- 返回 list[(bytes, ext)]，循环获取 NextDocument 直到 404
- 批量预览：全部页扫完后统一展示，用户可翻页/删除/调曝光
- PDF 默认合并为单个多页 PDF
- 支持自定义页面分组，每组生成独立文件

### 曝光层

**apply_exposure 扩展**
- 新增参数：gamma (float, 0.1-3.0), shadows (int, -100~100), highlights (int, -100~100), channel_gains (tuple[float, float, float])
- 处理顺序：channel_gains → shadows/highlights → gamma → brightness/contrast → auto stretch
- 保持 PIL Image 级操作，无 bytes 往返

**直方图显示**
- PreviewDialog 中用 Tkinter Canvas 绘制
- RGB 模式显示三通道叠加（红/绿/蓝半透明），灰度模式显示单通道
- 随曝光参数变化实时刷新

**预设系统**
- 内置预设：文字增强、照片还原、去灰底、旧文件修复
- 用户可保存当前参数为命名预设
- 存储于 presets.json

### 发现层

**WSD 发现**
- 独立模块 wsd_engine.py
- 与 mDNS 并行运行，结果按 IP 去重合并
- 发现结果统一转换为 ScannerInfo 对象

### 平台层

**平台抽象**
- platform_engine.py 定义统一 scan() → (bytes, ext) 接口
- 运行时 sys.platform 检测：Windows → WIA，macOS → ImageCapture/SANE
- GUI 层（CustomTkinter）已跨平台，无需修改

### CLI 层

**命令行接口**
- argparse 子命令：scan / discover / status / history
- 复用 escl_engine / wia_engine 核心函数
- 输出格式可选 human-readable 或 JSON

### 配置层

**文件拆分**
- scan_config.json — 基础设置（主题/分辨率/颜色/格式/来源/已保存打印机）
- presets.json — 曝光预设（内置 + 用户自定义）
- history.json — 扫描历史（append-only 日志）
- cache_config.json — 缓存策略（上限/触发线/清理量）

### 测试接缝

**现有接缝（优先使用）**
- escl_engine 函数级接口（execute_scan, apply_exposure, discover_scanners 等）— 纯函数，易于单元测试
- wia_engine 函数级接口（wia_scan, try_wia_scan）— 返回 (bytes, ext)，可 mock
- 新增 platform_engine 统一接口 — 可 mock 平台后端
- 新增 wsd_engine 发现接口 — 返回 ScannerInfo list，可 mock

**新增接缝**
- cache_manager 模块 — 缓存读写/清理逻辑，可独立测试
- preset_manager 模块 — 预设加载/保存/匹配，纯数据操作
- history_manager 模块 — 历史记录 append/查询，文件 I/O

## Testing Decisions

**测试原则**
- 只测外部行为，不测实现细节
- 优先使用现有函数级接缝
- 引擎层函数做单元测试（输入 bytes → 验证输出 bytes）
- 缓存/预设/历史管理做集成测试（文件 I/O + 清理逻辑）

**测试模块**
- escl_engine: apply_exposure 各模式、execute_multipage_scan、FORMAT_MIME/MIME_EXT 映射
- cache_manager: 写入/读取/清理/FIFO 顺序/触发线检测
- preset_manager: 加载/保存/内置预设/用户预设
- history_manager: append/查询/文件格式
- platform_engine: 运行时检测逻辑
- wsd_engine: 发现结果解析/去重

## Out of Scope

- 多页 WIA 扫描（WIA 设备通常无 ADF，保持单页）
- 扫描文件的 OCR 文字识别
- 云存储集成（OneDrive/Google Drive 直接保存）
- 网络打印/复印功能
- 移动端（iOS/Android）支持
- 扫描文件的网络共享/多人协作

## Further Notes

### 实现优先级
1. **P0 — 核心改造**: 磁盘缓存架构 + apply_exposure 扩展 + 配置拆分
2. **P1 — 多页 ADF**: execute_multipage_scan + 批量预览 + 页面分组
3. **P1 — 曝光增强**: 直方图 + Gamma/阴影高光/通道增益 + 预设系统
4. **P2 — 并发扫描**: 多线程并发 + 独立缓存子目录
5. **P2 — 扫描历史**: history.json + GUI 历史列表
6. **P3 — WSD 发现**: wsd_engine.py + 并行发现 + 去重
7. **P3 — CLI 模式**: argparse 子命令 + JSON 输出
8. **P3 — macOS 支持**: platform_engine.py + ImageCapture 后端

### 向后兼容
- execute_scan 签名不变，现有 GUI 调用不受影响
- scan_config.json 格式不变，旧配置文件可继续使用
- auto_exposure / manual_exposure 的 bytes 接口保留（向后兼容）
