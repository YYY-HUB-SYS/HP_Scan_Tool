# ADR-0012: 平台抽象层采用运行时检测

## 状态
已接受 (2026-07-16)

## 决策
- 新建 `platform_engine.py`，定义统一接口 `scan() → (bytes, ext)`
- 运行时通过 `sys.platform` 检测 OS，动态选择后端：
  - Windows → WIA (pywin32 COM)
  - macOS → ImageCapture (pyobjc) 或 SANE
- GUI 层（CustomTkinter）已跨平台，无需修改
- 同一份代码跨平台运行，不需要构建时分离

## 理由
- 运行时检测更灵活，开发时不需要切换代码
- CustomTkinter 已处理 GUI 跨平台，引擎层保持一致的设计
- 统一接口保证 GUI 调用方不需要关心平台差异
