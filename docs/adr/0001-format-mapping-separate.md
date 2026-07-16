# ADR-0001: 格式映射保持双引擎独立

## 状态
已接受 (2026-07-16)

## 背景
escl_engine 使用 FORMAT_MIME / MIME_EXT 做 MIME ↔ 扩展名映射，wia_engine 使用 _WIA_FORMAT_GUID 做扩展名 → WIA COM GUID 映射。两者描述同一组格式但处于不同抽象层。

## 决策
不引入统一的 FormatRegistry。两个引擎各自管理各自的格式映射。

## 理由
- WIA GUID 是 Windows COM 层面的标识符，MIME 是互联网标准，属于不同抽象层
- 强行合并会增加跨层耦合，违反各引擎的封装边界
- 两引擎的格式集合不完全相同（WIA 支持 BMP，eSCL 不支持）
- 合并后的 FormatRegistry 需要条件分支判断目标引擎，反而更复杂
