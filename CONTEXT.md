# HP Scan Tool — 领域术语表

## 核心概念

**eSCL (AirScan)**
HP 网络扫描协议，基于 HTTP+XML。扫描仪通过 mDNS 注册服务，客户端通过 REST 式 API 交互：ScannerCapabilities → ScannerStatus → ScanJobs → NextDocument。

**WIA (Windows Image Acquisition)**
Windows 本地扫描驱动接口，作为 eSCL 不可用时的降级方案。仅支持 USB 连接设备。

**ScannerInfo**
扫描仪的完整信息载体：IP、端口、eSCL URL、能力参数（分辨率/颜色模式/格式/ADF/双面）。

**ScanJob**
一次扫描任务的生命周期：POST 创建 → 轮询状态 → GET NextDocument 获取图像数据。

## 发现与连接

**mDNS 发现**
通过 zeroconf 监听 `_uscan._tcp.local.` 和 `_scanner._tcp.local.` 服务，自动发现局域网内的 eSCL 扫描仪。

**探活 (Probe)**
向指定 IP 发送 HTTP 请求检测 eSCL 服务是否可用，返回基础 URL（`/eSCL/` 或 `/`）。

**能力查询 (Capabilities)**
从 `/eSCL/ScannerCapabilities` XML 解析扫描仪支持的全部参数：分辨率列表、颜色模式、输出格式、ADF/双面支持等。

## 扫描参数

**来源 (Source / InputSource)**
扫描输入来源：平板 (Platen) 或 输稿器 (Feeder/ADF)。

**颜色模式 (ColorMode)**
RGB24（彩色）、Grayscale8（灰度）、BlackAndWhite1（黑白）。

**输出格式 (DocumentFormat)**
扫描数据的 MIME 格式：image/jpeg、image/png、image/tiff、application/pdf。

## 持久化

**配置 (Config)**
scan_config.json 文件，保存用户偏好：分辨率、颜色模式、格式、来源、已保存的打印机 IP 列表和自定义名称。

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
支持多台打印机同时扫描，每个任务独立缓存子目录。

## 发现协议

**WSD (Web Services Discovery)**
微软推的设备发现协议，SOAP over WS-Discovery。与 mDNS 并行运行，结果按 IP 去重合并，最大化发现率。实现在 `wsd_engine.py`。

## 多页扫描

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
