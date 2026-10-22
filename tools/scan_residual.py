r"""安装/卸载**残留扫描** —— HP Scan Tool

★ 借鉴 `Windows_Keeper` 的 `WinKeeper/scan_residual.bat` 思路（**只读借鉴，未改动对方任何文件**）：
  「装完/卸完必须扫一遍残留，有残留就报出来」——比"看着没问题"可靠。

★ 扫什么（都是**用户能看见**的东西）
  ① 注册表：控制面板的卸载条目（HKCU\...\Uninstall\{AppId}_is1）
  ② 文件系统：安装目录 %LOCALAPPDATA%\Programs\HP Scan Tool
  ③ 快捷方式：开始菜单组 + 桌面
  ④ 进程：还在跑的 HP_Scan_Tool.exe
  ⑤ 临时目录：%TEMP% 里的 _MEI*（onefile 版每次启动解压出来的）
     与 is-*（Inno 安装器/卸载器自己的自解压临时目录）

★ 用法
    python tools/scan_residual.py                    # 默认按「已安装」判定
    python tools/scan_residual.py --expect-removed   # 按「已卸载」判定（全都不该在）
    python tools/scan_residual.py --strict           # 有不该在的残留就退出码 1（可进 CI）

★ 两种预期（否则「已安装」会被误报成残留）
  · `--expect-installed`（默认）：安装目录 / 卸载条目 / 快捷方式**应该在**；
    `_MEI*`（onefile 每次启动解压）与 `is-*`（Inno 自解压临时目录）**不该在**。
  · `--expect-removed`：上面所有东西**都不该在**。
"""
import argparse
import glob
import os
import subprocess
import sys

APP_DIR_NAME = "HP Scan Tool"
APP_ID = "{7C3E9A21-4D58-4F6B-9C10-2A6E5D8F1B47}_is1"
UNINSTALL_KEY = (r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
                 r"\{7C3E9A21-4D58-4F6B-9C10-2A6E5D8F1B47}_is1")
TEMP = os.environ.get("TEMP", os.environ.get("TMP", ""))


def reg_key_exists(path):
    try:
        r = subprocess.run(["reg", "query", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="有不该在的残留就以退出码 1 结束")
    ap.add_argument("--expect-removed", action="store_true",
                    help="按「已卸载」判定（所有痕迹都不该在）")
    args = ap.parse_args()
    expect_installed = not args.expect_removed

    findings = []
    print("=== HP Scan Tool 残留扫描（预期：%s）===" %
          ("已安装" if expect_installed else "已卸载"))

    # ① 注册表
    has_key = reg_key_exists(UNINSTALL_KEY)
    print("① 控制面板卸载条目: %s" % ("在" if has_key else "无"))
    if has_key and not expect_installed:
        findings.append("注册表卸载条目仍在: %s" % UNINSTALL_KEY)

    # ② 安装目录
    inst = os.path.join(os.environ["LOCALAPPDATA"], "Programs", APP_DIR_NAME)
    if os.path.isdir(inst):
        n = sum(len(f) for _r, _d, f in os.walk(inst))
        size = dir_size(inst)
        print("② 安装目录: 在（%d 个文件 / %.1f MiB）" % (n, size / 1048576.0))
        if not expect_installed:
            findings.append("安装目录仍在: %s" % inst)
    else:
        print("② 安装目录: 无")
        if expect_installed:
            findings.append("安装目录不在（预期已安装）: %s" % inst)

    # ③ 快捷方式
    start = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows",
                         "Start Menu", "Programs")
    links = [p for p in glob.glob(os.path.join(start, "**", "*.lnk"), recursive=True)
             if "HP Scan Tool" in os.path.basename(p)]
    desk = [p for p in glob.glob(os.path.join(os.environ["USERPROFILE"], "Desktop", "*.lnk"))
            if "HP Scan Tool" in os.path.basename(p)]
    print("③ 快捷方式: 开始菜单 %d 个 / 桌面 %d 个" % (len(links), len(desk)))
    if not expect_installed:
        for p in links + desk:
            findings.append("快捷方式残留: %s" % p)
    elif not links:
        findings.append("开始菜单快捷方式不在（预期已安装）")

    # ④ 进程
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq HP_Scan_Tool.exe"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=20).stdout
        running = [l for l in out.splitlines()
                   if "HP_Scan_Tool.exe" in l and "没有" not in l]
    except Exception:
        running = []
    print("④ 进程: %s" % ("仍在跑 %d 个" % len(running) if running else "无"))
    for l in running:
        findings.append("进程仍在: %s" % l.strip())

    # ⑤ 临时目录（两种预期下都**不该在**）
    mei = glob.glob(os.path.join(TEMP, "_MEI*"))
    ist = glob.glob(os.path.join(TEMP, "is-*"))
    print("⑤ 临时目录: _MEI* %d 个（onefile 每次启动解压出来的）/ is-* %d 个"
          "（Inno 安装器/卸载器的自解压临时目录）" % (len(mei), len(ist)))
    for p in mei:
        findings.append("临时目录残留: %s" % os.path.basename(p))
    for p in ist:
        # 只把**属于本项目**的 is-* 算残留（含 HP_Scan_Tool_Setup* / _unins*）
        names = [f for _r, _d, fs in os.walk(p) for f in fs]
        if any(n.startswith("HP_Scan_Tool_Setup") or n.startswith("_unins")
               for n in names):
            findings.append("临时目录残留（本项目）: %s" % os.path.basename(p))
        else:
            print("    （跳过非本项目的 %s）" % os.path.basename(p))

    print()
    if findings:
        print("★ 发现 %d 项残留：" % len(findings))
        for f in findings:
            print("   - %s" % f)
        if args.strict:
            return 1
    else:
        print("✔ 无残留")
    return 0


if __name__ == "__main__":
    sys.exit(main())
