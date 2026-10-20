# D1 真机基准（改造前）— 2026-10-20

> ⚠️ **本文档含内网信息**（设备 IP / 内部路径 / 远端地址）。仓库当前只推送内网 Gitea，故保留这些值以便真机复现与对照。**若将来要推 GitHub 等公开平台，须先把 IP 泛化（如 `17.17.171.x`）并移除内部路径与远端地址。**
> **泛化时的完整清单（共 12 处）**：本目录 `docs/verify/`（本文件 + `regression_D2.md`）、`docs/audit/`（3 份）、`docs/design/`（3 份）、`docs/superpowers/specs/`（1 份）、`docs/superpowers/plans/`（1 份）—— 这 10 份文档各自第 3 行都带本提示；**另有 2 处是代码注释**（均在 `escl_engine.py`）：
> ① 「某些老打印机（实测 17.17.171.203 / .208）把 ScannerCapabilities 301 到 …」
> ② 「实测 2026-10-21（E2E 真机 17.17.171.111）：上一个 ScanJob 刚被 DELETE，紧接着的建 job 请求就 …」（503 退避说明）
> 泛化时一并处理。
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
| 像素尺寸（A4 画布口径 = **PDF 产物**口径） | 2480 × 3507 |
| 像素尺寸（原始扫描幅面口径 = **图片产物** jpg/png/tiff 口径） | 2550 × 3508 |
| 缓存文件名 | page_001.jpg |
| 落盘文件名 | HP_Scan_20261020_210142_001.jpg |
| 产物字节数 | 148575 |
| 耗时 | 12.9 秒 |

> **⚠️ 2026-10-21 订正（历史实测值照录不改，仅拆开「像素尺寸」的判定口径）**：
> 「像素尺寸」此前只有一个值 **2480 × 3507**，实际混着**两套口径**：
> - **PDF 产物 → A4 画布口径 = 2480 × 3507**：`save_manager._a4_canvas` 的画布
>   （`int(210/25.4*300)` × `int(297/25.4*300)`），**等比缩放 + 居中 + 白边**，
>   **不裁切**；
> - **图片产物（jpg/png/tiff）→ 原始扫描幅面口径 = 2550 × 3508**：
>   `save_manager` 的图片分支**不经过** `_a4_canvas`（只有 PDF 分支经过），
>   图片格式没有「物理页面」这个概念 ⇒ 保留设备原始幅面，不缩放不加白边。
>
> ★ **本文件 D1 那次跑出来的 .jpg 也量成了 2480 × 3507，是因为
> `tools/verify_scan.py` 的 `_a4_crop` 把产物**原地覆写**成 A4 画布之后
> 才量的**（见下方「设备能力值」一节的出处说明）—— 那是**验证脚本的口径**，
> **不是 app 侧产物行为**。app 侧的 jpg/png/tiff 产物是 2550 × 3508。

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
| 落盘文件像素 | 2480 × 3507（**A4 画布口径**，= PDF 产物口径；见上方订正说明） |
| 落盘文件格式 | JPEG |

> **⚠️ 2026-10-21 订正（历史实测值照录不改，仅拆口径）**：上面「落盘文件像素
> 2480 × 3507」量的是 **`tools/verify_scan.py` 原地 A4 裁剪之后**的产物
> （该脚本覆写产物后才采集元数据），**落在 A4 画布口径上**。
> **app 侧图片产物（jpg/png/tiff）的口径是原始扫描幅面 2550 × 3508**
> （= 设备 `MaxWidth`×`MaxHeight`，图片分支不走 `_a4_canvas`）。
> 两套口径的差别只在「是否归一到 A4 画布」，**不影响**页数 / 格式 / 文件命名
> / 参数 / eSCL URL 这几项硬指标。

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

**本次未采集**。采集时该机输稿器为空（`AdfState = ScannerAdfEmpty`），Feeder 路径无法出纸。

实测记录（2026-10-20，两次）：

1. 跑 `tools/verify_scan.py --ip 17.17.171.208 --pages 2 --source Feeder` → 退出码 3，stderr：

   ```
   ERROR: 页数不符 — 期望 2 页，实际 0 页
   ERROR: 扫描异常：创建扫描任务失败 HTTP 409:
   ```

   409 = 打印机上残留一个未完成的 ScanJob（此前某次扫描异常中断留下的）。该型号不支持列举（`GET /eSCL/ScanJobs` → 405）与整体删除（`DELETE /eSCL/ScanJobs` → 405），客户端无法主动清理，只能等打印机侧 TTL 回收（实测约 15-20 分钟）。

2. 残留回收后，绕过脚本直接建 ScanJob 探测（Platen 与 Feeder 两种 `InputSource`）：

   | 输入源 | 设备响应 |
   |--------|----------|
   | Platen | **201 Created**（job 创建成功） |
   | Feeder | **503 Service Unavailable** |

   即：**输稿器空载时打印机在创建任务阶段就返回 503**（设备在等纸），与 409（残留 job）是两种不同状态。

> **采集 ADF 基准的前置条件**：往 17.17.171.208 的输稿器里放入 2-3 页纸，使 `AdfState` 变为 `ScannerAdfLoaded`。D1 与 D2 必须投入**同样张数**的纸，否则无法做同条件对照。
>
> 该机的 `ScannerCapabilities` 另有一个已知限制：返回 301 跳 HTTPS 且 TLS 握手失败，能力查询走回退路径，`max_width` 取默认值 2480 而非设备真值。因此 ADF 场景的页宽**以实跑 `pages[0].width` 为准**，不能照搬平板那台的数值。

## 改造后必须一致的项

改造后跑同一条命令产出 D2 的 `report.json`，以下各项必须与上表逐项对齐。

- 页数：`result.page_count == 1`（平板单页场景），且必须满足 `result.page_count == params.requested_pages`，否则脚本以退出码 3 失败
- 像素尺寸（**两套口径**，2026-10-21 订正）：
  - **PDF 产物**（A4 画布）：**2480 × 3507**
  - **图片产物**（jpg/png/tiff，原始扫描幅面）：**2550 × 3508**
- 格式：`pages[0].format == "JPEG"`，且 `result.ext == "jpg"`
- 缓存文件命名 `page_NNN.ext`：实测 `page_001.jpg`
- 落盘文件命名 `HP_Scan_<时间戳>_NNN.ext`：实测 `HP_Scan_20261020_210142_001.jpg`
- eSCL URL：`device.escl_url == "http://17.17.171.111:80/eSCL/"`
- 扫描参数：`params.resolution == 300`、`params.source == "Platen"`、`params.requested_pages == 1`
- 型号字段：`device.model == ""`（当前为空；若 D2 有变更须显式标注）

**仅作参考、不作为一致性判据**的两项——它们与纸张内容强相关，换一张纸就可能越界：

| 项 | D1 实测 | 说明 |
|----|---------|------|
| 扫描耗时 | 12.9 秒 | 随设备负载与纸张反光变化，通常同一量级即可 |
| 产物字节数 | 148575 | 随纸张内容与压缩率变化，**不能用 ±50% 之类的固定容差判定一致** |

判断 D2 是否回归，应以上面那组**硬指标**（页数、像素尺寸、格式、文件命名、参数、eSCL URL）为准；耗时与字节数只用来观察量级是否异常。

> **⚠️ 2026-10-21 订正（判定口径说明，不改任何实测值）**：
> - `report.json` 的 `pages[0].width` / `height` 量的是 **`tools/verify_scan.py`
>   原地 A4 裁剪之后**的值（该脚本 `_a4_crop` 覆写产物后才采集元数据），
>   ⇒ 它落在**第一套口径（A4 画布 2480 × 3507）**上。
> - 裁剪**前**的原始扫描幅面在磁盘上不存在，2026-10-21 起由
>   **`pages[0].raw_width` / `raw_height`** 单独报出（`tools/verify_scan.py`
>   新增字段，**默认行为与退出码语义不变**）；`--no-a4-crop` 开关可让产物
>   保持原始幅面（用于「只量原始幅面」的场景）。
> - **app 侧产物**的口径：PDF = A4 画布 2480 × 3507；
>   图片（jpg/png/tiff）= 原始扫描幅面 2550 × 3508（图片分支不走 `_a4_canvas`）。

---

## D2 真机回归结果（改造后）— 2026-10-21 00:36

> 📄 **本章节已于 2026-10-21 拆出为独立文件：[`regression_D2.md`](regression_D2.md)** —— 那是实施计划「文件结构 → 新建」表约定的路径，此前 D2 记录被追加在本文件里。
> **内容逐字搬运，未重跑真机、未改动任何实测值与判定**；结论仍为 **PASS**。D1 的对照列在本文件上半部分，D2 的实测列在 `regression_D2.md`。
