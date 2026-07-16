# ADR-0010: WSD 发现与 mDNS 并行运行、按 IP 去重

## 状态
已接受 (2026-07-16)

## 决策
- WSD 发现作为独立模块 `wsd_engine.py` 实现
- 与现有 mDNS 发现并行运行，结果按 IP 去重合并
- 不替代 mDNS，也不作为降级方案

## 理由
- 两种协议覆盖面不同：部分打印机只注册 mDNS，部分只注册 WSD
- 并行运行最大化发现率
- 按 IP 去重简单可靠，不需要优先级逻辑
- 独立模块保持与 escl_engine 的解耦，wsd_engine 只负责发现，不负责扫描

## 实现备注
- WSD 协议基于 SOAP over WS-Discovery，比 mDNS 复杂
- 建议先用 /research 技能调研 WSD 协议细节和现有 Python 实现
- 发现结果统一转换为 ScannerInfo 对象，与 mDNS 结果无缝合并
