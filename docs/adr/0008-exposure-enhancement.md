# ADR-0008: 曝光调整增强 — 直方图 + 全维度调整 + 预设系统

## 状态
已接受 (2026-07-16)

## 决策
在现有三种模式（关闭/自动/手动）基础上增强：

### 直方图显示
- PreviewDialog 中展示实时像素分布图
- 使用 PIL 计算 histogram + Tkinter Canvas 绘制
- RGB 模式下显示三通道叠加（红/绿/蓝），灰度模式显示单通道
- 随曝光参数变化实时刷新

### 更多调整维度
- **Gamma 校正** — 非线性亮度调整，适合修复偏暗/偏亮的扫描件
- **阴影/高光分离** — 独立控制暗部和亮部，比单纯的亮度/对比度更精准
- **RGB 单通道调整** — 修复偏色（如发黄的旧文件降低蓝色通道）

### 预设系统
- 内置预设：「文字增强」「照片还原」「去灰底」「旧文件修复」
- 用户可自定义预设，保存当前所有曝光参数为命名预设
- 预设存储为 JSON 文件（`%APPDATA%\HP_Scan_Tool\presets.json`，与 scan_config.json 同级）
- 预设数据结构：`{name, mode, brightness, contrast, gamma, shadows, highlights, channel_gains: [r, g, b], builtin: bool}`

> 2026-10-20 订正：实现采用 `channel_gains` 三元素数组 + `builtin` 标记。
>
> 本节原先写的 `{..., r_gain, g_gain, b_gain}` 是设计稿，与实际实现不符；
> 代码（`preset_manager.py` / `exposure.apply_exposure`）一直用的是
> `channel_gains` 三元素数组，且 `builtin` 字段用于区分「内置不可删」与用户自定义。
> 以代码为准订正本 ADR —— 为迁就文档去改能跑的代码不划算。
>
> - `channel_gains`: 三元素数组 `[r, g, b]`，默认 `[1.0, 1.0, 1.0]`；
>   落盘为 JSON 时是数组，传入 `apply_exposure` 时规范化为 tuple `(1.0, 1.0, 1.0)`
>   （列表与元组比较不相等，不规范化会误触发通道增益分支）。
> - `builtin`: bool，`True` = 内置预设（不可删除），`False` = 用户自定义。
>   `preset_manager.load_presets()` 会把用户预设强制置为 `builtin=False`。

### apply_exposure 扩展
统一入口 apply_exposure 扩展参数：
- gamma: float (0.1 - 3.0, 默认 1.0)
- shadows: int (-100 ~ 100, 默认 0)
- highlights: int (-100 ~ 100, 默认 0)
- channel_gains: tuple[float, float, float] (默认 (1.0, 1.0, 1.0))

## 理由
- 直方图是扫描工具标配，提供数据依据而非盲调
- 更多维度覆盖不同原稿类型（发黄旧文件、过曝照片、偏色文档）
- 预设系统对批量扫描场景价值极高，一键应用常用参数
- 实现成本低：PIL 原生支持 histogram/gamma，预设就是 JSON
