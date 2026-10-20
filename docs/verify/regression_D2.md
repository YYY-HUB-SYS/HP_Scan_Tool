# D2 真机回归（改造后）— 2026-10-21 00:36

> ⚠️ **本文档含内网信息**（设备 IP / 内部路径 / 远端地址）。仓库当前只推送内网 Gitea，故保留这些值以便真机复现与对照。**若将来要推 GitHub 等公开平台，须先把 IP 泛化（如 `17.17.171.x`）并移除内部路径与远端地址。**
> **泛化时的完整清单（共 11 处）**：见 [`baseline_D1.md`](baseline_D1.md) 第 4 行（本文档是其中一份）；本文件含真实设备 IP `17.17.171.111` / `17.17.171.208`。
> **来源**：本文件的正文原是 `baseline_D1.md` 内的「D2 真机回归结果（改造后）」章节，2026-10-21 按实施计划「文件结构 → 新建」表约定的路径拆出为独立文件。**内容逐字搬运，未重跑真机、未改动任何实测值与判定。**

对照基准（改造前）：[`baseline_D1.md`](baseline_D1.md)

---

## D2 真机回归结果（改造后）— 2026-10-21 00:36

采集命令与 D1 完全相同：

```bash
.\.venv-local\Scripts\python.exe tools/verify_scan.py --ip 17.17.171.111 --pages 1 \
    --out .verify_out/D2_platen --resolution 300 --source Platen
```

**结论：PASS —— 硬指标逐项一致，无回归；且型号解析修复在真机上确认生效。**

| 判据 | D1（改造前） | D2（改造后） | 判定 |
|------|--------------|--------------|------|
| `result.page_count` | 1 | 1 | ✅ 一致 |
| `pages[0].width` × `height` | 2480 × 3507 | 2480 × 3507 | ✅ 逐像素一致 |
| `pages[0].format` | JPEG | JPEG | ✅ |
| `result.ext` | jpg | jpg | ✅ |
| 缓存文件命名 | `page_001.jpg` | `page_001.jpg` | ✅ |
| 落盘文件命名 | `HP_Scan_20261020_210142_001.jpg` | `HP_Scan_20261021_003626_001.jpg` | ✅ 同规则 |
| `device.escl_url` | `http://17.17.171.111:80/eSCL/` | 同 | ✅ |
| `params.resolution` / `source` / `requested_pages` | 300 / Platen / 1 | 同 | ✅ |
| **`device.model`** | **`""`（空）** | **`"HP LaserJet MFP M232dwc"`** | ⚠️ **有变更，原因是修复而非回归**（见下） |
| 扫描耗时（参考项） | 12.9 秒 | 13.7 秒 | ✅ 同量级 |
| 产物字节数（参考项） | 148575 | 148371 | ✅ 差 0.14%（同纸重扫正常波动） |

**型号字段变更说明**：D1 时 `device.model` 为空，是 `escl_engine` 早期**只匹配 `Model` 标签**、
而该机型实际上报 `pwg:MakeAndModel` 所致；本轮修复合机型解析后，D2 拿到了真实型号
`HP LaserJet MFP M232dwc`。**这是修复，不是回归**，故按上文"若 D2 有变更须显式标注"的要求在此标注。

### D2 附加验证：新的「打印机只出图片、PDF 本地合成」链路真机端到端

在同一台真机上额外跑了一遍完整链路（扫描 → 缓存 → `save_manager` 本地合成 PDF → 独立校验）：

| 步骤 | 实测结果 |
|------|---------|
| 扫描 | 13.3 秒，`ext=jpg`，151903 字节 |
| 纠偏角检测 | 墨迹占比 **0.3645%** ⇒ `estimate_skew` 返回 **None**（该页属近空白，**空白页保护正确生效**，不误判角度） |
| 页元数据落库 | 读回 `width=2480 height=3508 deskew_angle=0.0` ✅ |
| `save_manager.save_pages(..., ext="pdf")` | 返回 `SaveResult`，`len=1`、`failed_pages=0`、`saved_page_count=1` ✅ |
| PDF 产物 | 152600 字节，魔数 **`%PDF-`** ✅ |
| pypdf 独立打开 | **1 页**，mediabox **595.2 × 841.68 pt = 标准 A4** ✅ |
| PDF 内嵌图片还原 | `save_manager._extract_pdf_image` 取出 **2480 × 3507 RGB** ⇒ **内容无丢失** ✅ |

> ⚠️ 平板扫描**不出纸**（纸留在玻璃上），本次回归未产生任何出纸动作；ADF 路径按用户指示本轮不测。
