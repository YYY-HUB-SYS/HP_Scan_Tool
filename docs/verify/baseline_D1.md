# D1 真机基准（改造前）— 2026-10-20

> 由 `tools/verify_scan.py` 生成。改造后 D2 必须与本文件逐项一致。

采集命令：

```bash
.\.venv-local\Scripts\python.exe tools/verify_scan.py --ip 17.17.171.111 --pages 1 --out .verify_out/D1_platen
```

原始数据：`.verify_out/D1_platen/report.json`（本表数值全部抄自该文件）。

## 平板单页（17.17.171.111）

| 项 | 基准值 |
|----|--------|
| eSCL URL | http://17.17.171.111:80/eSCL/ |
| 型号 | `""`（report.json 中 `device.model` 为空串，见下方「型号字段为空」说明） |
| 页数 | 1 |
| 像素尺寸 | 2480 × 3507 |
| 缓存文件名 | page_001.jpg |
| 落盘文件名 | HP_Scan_20261020_201425_001.jpg |
| 产物字节数 | 147991 |
| 耗时 | 14.1 秒 |

同一份 report.json 中的其他实测值：

| 项 | 基准值 | report.json 路径 |
|----|--------|------------------|
| 设备 IP | 17.17.171.111 | `device.ip` |
| ADF 能力 | false（该机型无输稿器） | `device.has_adf` |
| 分辨率 | 300 | `params.resolution` |
| 扫描源 | Platen | `params.source` |
| 请求页数 | 1 | `params.requested_pages` |
| 实际页数 | 1 | `result.page_count` |
| 扩展名 | jpg | `result.ext` |
| 图片格式 | JPEG | `pages[0].format` |
| 页序 | 1 | `pages[0].index` |
| 耗时 | 14.1 | `result.elapsed_sec` |

补充实测（不写入 report.json，来自同一次运行的产物核对）：

| 项 | 实测值 |
|----|--------|
| 缓存文件字节数 | 142004 |
| 落盘文件字节数 | 147991 |
| 落盘文件像素 | 2480 × 3507 |
| 落盘文件格式 | JPEG |
| 裁剪前原始尺寸 | 2550 × 3508（设备 Platen MaxWidth/MaxHeight，A4 居中裁剪前） |

### 型号字段为空

`device.model` 实测为空字符串。原因是 `escl_engine.fetch_capabilities` 只在 XML 里找 `Model` 标签，而该机型上报的是 `pwg:MakeAndModel`（实测值 `HP LaserJet MFP M232dwc`，序列号 `VNP6N40672`），因此未命中。

D1 基准按实际跑出的 report.json 记录为 `""`，不做美化。改造后 D2 若仍为 `""` 则与基准一致；若改造过程中修复了型号解析，则属于预期内的行为变更，需在 D2 文档中显式标注。

## ADF 多页（17.17.171.208）

> 待采集：需要人工往输稿器放 2-3 页纸，由控制方补齐。

## 改造后必须一致的项

改造后跑同一条命令产出 D2 的 `report.json`，以下各项必须与上表逐项对齐。

- 页数：`result.page_count == 1`（平板单页场景）
- 像素尺寸：`pages[0].width == 2480` 且 `pages[0].height == 3507`
- 格式：`pages[0].format == "JPEG"`，且 `result.ext == "jpg"`
- 缓存文件命名 `page_NNN.ext`：实测 `page_001.jpg`
- 落盘文件命名 `HP_Scan_<时间戳>_NNN.ext`：实测 `HP_Scan_20261020_201425_001.jpg`
- 扫描耗时量级：`result.elapsed_sec` 为 14.1 秒（不要求完全相同，±50% 以内，即 7.1 ~ 21.2 秒）
- 产物字节数：147991 字节（±50% 以内，即 73996 ~ 221987 字节）
- eSCL URL：`device.escl_url == "http://17.17.171.111:80/eSCL/"`
- 扫描参数：`params.resolution == 300`、`params.source == "Platen"`、`params.requested_pages == 1`
- 型号字段：`device.model == ""`（当前为空；若 D2 有变更须显式标注）
