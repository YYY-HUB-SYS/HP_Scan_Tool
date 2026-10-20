# D1 真机基准（改造前）— 2026-10-20

> 由 `tools/verify_scan.py` 生成。改造后 D2 必须与本文件逐项一致。

采集命令：

```bash
.\.venv-local\Scripts\python.exe tools/verify_scan.py --ip 17.17.171.111 --pages 1 --out .verify_out/D1_platen
```

退出码约定：`0` = 扫描成功且实际页数与 `--pages` 期望值一致；`2` = 探测不到 eSCL 服务；`3` = 实际页数与 `--pages` 不符，或扫描过程抛异常未取到页（详见「页数校验语义」）。

原始数据：`.verify_out/D1_platen/report.json`（**本地验证产物，未入库**）。为使本文档自包含，全文原样嵌入如下：

```json
{
  "device": {
    "ip": "17.17.171.111",
    "model": "",
    "escl_url": "http://17.17.171.111:80/eSCL/",
    "has_adf": false
  },
  "params": {
    "resolution": 300,
    "source": "Platen",
    "requested_pages": 1
  },
  "result": {
    "page_count": 1,
    "ext": "jpg",
    "elapsed_sec": 12.9
  },
  "pages": [
    {
      "index": 1,
      "cache_file": "page_001.jpg",
      "width": 2480,
      "height": 3507,
      "format": "JPEG",
      "saved_file": "HP_Scan_20261020_210142_001.jpg",
      "saved_bytes": 148575
    }
  ]
}
```

## 平板单页（17.17.171.111）

| 项 | 基准值 |
|----|--------|
| eSCL URL | http://17.17.171.111:80/eSCL/ |
| 型号 | `""`（report.json 中 `device.model` 为空串，见下方「型号字段为空」说明） |
| 页数 | 1 |
| 像素尺寸 | 2480 × 3507 |
| 缓存文件名 | page_001.jpg |
| 落盘文件名 | HP_Scan_20261020_210142_001.jpg |
| 产物字节数 | 148575 |
| 耗时 | 12.9 秒 |

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
| 耗时 | 12.9 | `result.elapsed_sec` |

补充实测（不写入 report.json，来自本次运行的产物核对）：

| 项 | 实测值 |
|----|--------|
| 缓存文件字节数 | 142179（运行期间从缓存任务目录 `20261020_210129_339287/page_001.jpg` 读取；缓存随后被清理，事后不可复核） |
| 落盘文件字节数 | 148575 |
| 落盘文件像素 | 2480 × 3507 |
| 落盘文件格式 | JPEG |

### 设备能力值（非本次运行产物）

| 项 | 能力值 |
|----|--------|
| 设备上报 Platen MaxWidth × MaxHeight | 2550 × 3508 |

> **出处说明**：2550 × 3508 是**设备能力值**，来自 `ScannerCapabilities` 的 `scan:MaxWidth` / `scan:MaxHeight`，**不是本次运行的产物实测值**。本次运行产物已被 `_a4_crop`（`tools/verify_scan.py:37-41`）原地覆写为 A4 裁剪结果，磁盘上不存在裁剪前尺寸的文件。**本次运行产物经 A4 裁剪后为 2480 × 3507**，做像素对照时以此为准。

### 型号字段为空

`device.model` 实测为空字符串。原因是 `escl_engine.fetch_capabilities` 只在 XML 里找 `Model` 标签，而该机型上报的是 `pwg:MakeAndModel`（实测值 `HP LaserJet MFP M232dwc`，序列号 `VNP6N40672`），因此未命中。

D1 基准按实际跑出的 report.json 记录为 `""`，不做美化。改造后 D2 若仍为 `""` 则与基准一致；若改造过程中修复了型号解析，则属于预期内的行为变更，需在 D2 文档中显式标注。

### 页数校验语义

`--pages` 是**期望页数**，不是截断上限。扫描结束后脚本校验 `实际页数 == --pages`，不符则：

- 实际抓到的页数**全部**如实写入 `report.json`（`params.requested_pages` 与 `result.page_count` 两个字段都保留），不做截断，以免掩盖打印机实际出了几页；
- 进程以**退出码 3** 结束，并在 stderr 打印 `ERROR: 页数不符 — 期望 N 页，实际 M 页`。

因此 D1 与 D2 两次采集必须投入**同样张数**的纸，否则脚本以退出码 3 失败，对照不会产生与改造无关的假差异。

扫描过程本身抛异常（如 eSCL 请求失败）时，脚本捕获异常、如实记录已取到的页数，并以退出码 3 结束，同时在 stderr 打印 `ERROR: 扫描异常：<原因>`。

## ADF 多页（17.17.171.208）

> 待采集：需要人工往输稿器放 2-3 页纸，由控制方补齐。

## 改造后必须一致的项

改造后跑同一条命令产出 D2 的 `report.json`，以下各项必须与上表逐项对齐。

- 页数：`result.page_count == 1`（平板单页场景），且必须满足 `result.page_count == params.requested_pages`，否则脚本以退出码 3 失败
- 像素尺寸：`pages[0].width == 2480` 且 `pages[0].height == 3507`
- 格式：`pages[0].format == "JPEG"`，且 `result.ext == "jpg"`
- 缓存文件命名 `page_NNN.ext`：实测 `page_001.jpg`
- 落盘文件命名 `HP_Scan_<时间戳>_NNN.ext`：实测 `HP_Scan_20261020_210142_001.jpg`
- 扫描耗时量级：`result.elapsed_sec` 为 12.9 秒（不要求完全相同，±50% 以内，即 6.5 ~ 19.4 秒）
- 产物字节数：148575 字节（±50% 以内，即 74288 ~ 222862 字节）
- eSCL URL：`device.escl_url == "http://17.17.171.111:80/eSCL/"`
- 扫描参数：`params.resolution == 300`、`params.source == "Platen"`、`params.requested_pages == 1`
- 型号字段：`device.model == ""`（当前为空；若 D2 有变更须显式标注）
