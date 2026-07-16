# ADR-0002: 多页 ADF 扫描使用独立函数

## 状态
已接受 (2026-07-16)

## 背景
当前 execute_scan 只获取一页 NextDocument 就返回。多页 ADF 扫描需要循环获取直到 404，返回类型从 (bytes, ext) 变为 list[bytes]，且需处理中途失败。

## 决策
新建 execute_multipage_scan 函数，不修改现有 execute_scan。

## 理由
- 单页和多页生命周期差异大，混在一起增加单页扫描的复杂度
- 多页需要不同的返回类型 list[bytes] 和错误处理策略（某页失败时的回退）
- 保持 execute_scan 简单，符合「一个函数做一件事」原则
