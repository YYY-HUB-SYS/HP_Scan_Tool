# 部署配置模板

> 将本文件复制为 `deploy-config.md` 并填写实际值，不要提交真实配置到版本库。

## 1. Git 仓库

```bash
# 远程仓库地址（内部 Gitea / GitHub / GitLab）
GIT_REMOTE=<SECRET_478a8206>

# 默认分支
GIT_BRANCH=main
```

## 2. 网络环境

```bash
# 打印机所在网段（用于 mDNS 发现范围）
LAN_SUBNET=x.x.x.x/x

# 默认打印机 IP 示例
DEFAULT_PRINTER_IP=x.x.x.x
```

## 3. 构建配置

```bash
# 项目根目录（绝对路径）
PROJECT_DIR=c:\Users\YQQ-Agent\Desktop\PC传输专用\HP_Scan_Tool

# 构建输出目录
DIST_DIR=dist

# PyInstaller 参数
PYINSTALLER_SPEC=HP_Scan_Tool.spec
```

## 4. 运行时配置

`scan_config.json` 中的示例值：

```json
{
  "saved_ips": [
    {"ip": "x.x.x.x", "model": "HP LaserJet MFP M232-M237"}
  ],
  "nicknames": {
    "x.x.x.x": "财务室打印机"
  }
}
```

## 5. 注意事项

- 本文件包含占位符，实际部署时替换为真实值
- 真实配置（含 IP、路径、token）禁止提交到公开仓库
- 内部部署时请将 `deploy-config.md` 加入 `.gitignore`
