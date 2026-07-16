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
- 预设存储为 JSON 文件（`presets.json`，与 scan_config.json 同级）
- 预设数据结构：`{name, mode, brightness, contrast, gamma, shadows, highlights, r_gain, g_gain, b_gain}`

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
