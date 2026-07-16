# ADR-0011: CLI 采用 argparse 子命令模式

## 状态
已接受 (2026-07-16)

## 决策
命令行接口采用 argparse 子命令模式：
- `hp_scan scan --ip IP [--resolution N] [--format FMT] [--source SRC] [--exposure MODE]`
- `hp_scan discover [--timeout N]`
- `hp_scan status --ip IP`
- `hp_scan history [--limit N]`

## 理由
- 子命令模式扩展性好，未来新增功能不撑爆参数空间
- 与 GUI 功能一一对应，用户容易理解
- 适合脚本集成和自动化场景
