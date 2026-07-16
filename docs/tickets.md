# HP Scan Tool v3.3 — 实施票据

按依赖顺序排列，blocker 在前。

---

## T1: 配置拆分 + 缓存管理器
**Blocked by:** 无
**交付:** 4 个配置文件独立运作 + 缓存目录自动读写/FIFO 清理/触发线检测/启动兜底

- 新建 cache_manager 模块：write(job_id, page, data) / read(job_id, page) / cleanup() / size()
- 缓存位置 `~/Documents/HP_Scans/.cache/`
- cache_config.json: 上限(1024MB)、触发线(95%)、清理量(150MB)
- scan_config.json / presets.json / history.json / cache_config.json 各自独立
- 向后兼容：旧 scan_config.json 可识别
- 启动时清理 >24h 残留缓存

---

## T2: apply_exposure 扩展 + 直方图显示
**Blocked by:** 无
**交付:** 曝光参数扩展到 gamma/阴影/高光/RGB 通道增益；PreviewDialog 显示实时直方图

- apply_exposure 新增 gamma, shadows, highlights, channel_gains 参数
- 处理顺序：channel_gains → shadows/highlights → gamma → brightness/contrast → auto stretch
- PreviewDialog 添加 Tkinter Canvas 直方图：RGB 三通道叠加 / 灰度单通道
- 直方图随参数变化实时刷新
- auto_exposure / manual_exposure bytes 接口保留（向后兼容）

---

## T3: 曝光预设系统
**Blocked by:** T1, T2
**交付:** 内置 4 个预设 + 用户自定义预设 + 预设选择 UI

- 内置预设：文字增强、照片还原、去灰底、旧文件修复
- 用户可保存当前参数为命名预设
- presets.json 存储（由 T1 的配置文件拆分支持）
- PreviewDialog 添加预设下拉选择器
- 选择预设后实时更新预览和滑块位置

---

## T4: 磁盘缓存集成扫描流程
**Blocked by:** T1
**交付:** 扫描数据写入磁盘缓存，预览从缓存加载缩略图，确认后复制到目标并清理缓存

- execute_scan 不变，在 GUI 层将返回的 bytes 写入缓存文件
- PreviewDialog 改为从缓存文件路径加载（而非 raw bytes）
- _confirm: 从缓存加载原图 → apply_exposure → 写入目标 → 清除缓存
- _cancel: 清除缓存目录
- 内存占用从全分辨率降为缩略图级别

---

## T5: 多页 ADF 扫描 + 批量预览
**Blocked by:** T4
**交付:** ADF 多页扫描 → 批量预览（翻页/删除/调曝光）→ 一次性保存

- escl_engine 新增 execute_multipage_scan → list[(bytes, ext)]
- 循环获取 NextDocument 直到 HTTP 404
- 每页数据写入缓存子目录
- MultiPagePreviewDialog: 缩略图列表 + 翻页导航
- 支持删除单页、全页/单页曝光调整
- 保存时按序写入目标目录

---

## T6: 多页保存 + 页面分组
**Blocked by:** T5
**交付:** PDF 默认合并多页；支持自定义页面分组，每组生成独立 PDF

- PDF 格式默认合并为单个多页 PDF（PIL Image.save multi-page）
- 用户可在页面间插入分割线标记分组
- 每个分组可单独命名
- 保存时按分组生成文件：`name_001.pdf`, `name_002.pdf`...
- 非 PDF 格式保存为独立编号文件：`page_001.jpg`, `page_002.jpg`

---

## T7: 并发扫描
**Blocked by:** T4
**交付:** 多台打印机同时扫描，各自独立缓存，各自弹出预览

- 扫描任务在独立线程中运行，独立缓存子目录
- UI 支持同时显示多个扫描进度
- 每个任务完成后独立弹出 PreviewDialog
- 状态栏显示活跃扫描任务数

---

## T8: 扫描历史
**Blocked by:** T1
**交付:** 每次扫描记录元数据到 history.json；GUI 提供历史列表对话框

- history_manager 模块：append(entry) / list(limit) / clear()
- 字段：时间戳、设备名/IP、页数、文件路径、扫描参数
- GUI 新增「扫描历史」按钮 → HistoryDialog
- 列表显示，双击打开文件或所在文件夹

---

## T9: WSD 发现协议
**Blocked by:** 无
**交付:** wsd_engine.py 实现 WS-Discovery，与 mDNS 并行运行，按 IP 去重

- wsd_engine.py: discover_wsd(timeout) → list[ScannerInfo]
- SOAP over WS-Discovery 协议实现
- GUI 发现流程同时启动 mDNS + WSD，合并结果按 IP 去重
- 发现结果统一为 ScannerInfo 对象

---

## T10: CLI 命令行模式
**Blocked by:** T1
**交付:** argparse 子命令入口，支持 scan / discover / status / history

- 新建 cli.py: argparse + 子命令
- `hp_scan scan --ip IP [--resolution N] [--format FMT] [--source SRC] [--exposure MODE] [--output DIR]`
- `hp_scan discover [--timeout N] [--format json|text]`
- `hp_scan status --ip IP [--format json|text]`
- `hp_scan history [--limit N] [--format json|text]`
- 复用 escl_engine / wia_engine 核心函数
- run.bat 添加 --cli 参数路由

---

## 依赖图

```
T1 (配置+缓存) ──┬── T3 (预设)
                 ├── T4 (缓存集成) ──┬── T5 (多页ADF) ── T6 (分组保存)
                 │                   └── T7 (并发扫描)
                 ├── T8 (扫描历史)
                 └── T10 (CLI)

T2 (曝光+直方图) ── T3 (预设)

T9 (WSD) — 独立
```

**可并行的起始票据:** T1, T2, T9（三者互不依赖）
