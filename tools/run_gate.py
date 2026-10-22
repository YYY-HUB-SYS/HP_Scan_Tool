# -*- coding: utf-8 -*-
r"""
交付门禁 — HP Scan Tool v4.0

**这道门禁以前只活在 `%TEMP%` 的临时脚本里**（清一次 `%TEMP%` 就没了，
下一个人得从头重写一遍，还容易把统计段写错）。现在固化进仓库，谁都能复跑。

跑什么：
  ① 全量 `pytest tests/ -q` × N（默认 3）
  ② 顺序鲁棒性 **Phase 3**：测试文件**随机排列** × M（默认 5，seed 可指定）
     —— 排的是"文件顺序"，用来暴露"某个文件必须先跑才不崩"的顺序依赖
  ③ 三条**零增量**判据（跑前跑后对比）
       a. `%TEMP%` 下 `hp_*` 临时目录数 增量 = 0（临时目录泄漏治理仍有效）
       b. `explorer.exe` 进程数 增量 = 0
          （★ 测试**不得**在用户屏幕上弹资源管理器窗口 —— 台账 §12.8）
       c. 真实用户 `%APPDATA%\HP_Scan_Tool\history.json` 的 SHA256 不变
          （★ 测试**不得**污染用户数据）

用法:
    python tools/run_gate.py                       # 默认：全量 ×3 + Phase3 ×5
    python tools/run_gate.py --rounds 1 --phase3 1 # 快速自检
    python tools/run_gate.py --skip-phase3         # 只跑全量 + 零增量
    python tools/run_gate.py --seed 20261021

退出码：0 = 全通过；1 = 有失败项（逐条打印）。

★ 两条实测踩过的坑，这里都规避了：
  ① **`%` 格式化里的字面百分号**：`"%TEMP%"` 会被当成格式符 ⇒
     `ValueError: unsupported format character 'T'`。
     ⇒ 本文件**一律用 f-string**，不混用 `%` 格式化。
  ② **子进程输出编码**：Windows 默认 gbk 解码 pytest 的 UTF-8 输出 ⇒
     `UnicodeDecodeError` ⇒ 末行读不到被误判 BAD。
     ⇒ 一律 `encoding="utf-8", errors="replace"`。
"""
import argparse
import hashlib
import os
import random
import subprocess
import sys
import time

if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TESTS = os.path.join(ROOT, "tests")
PY = sys.executable

ENV = dict(os.environ)
ENV["PYTHONIOENCODING"] = "utf-8"
ENV["PYTHONUTF8"] = "1"

# 失败项收集（模块级 ⇒ run_pytest 也能往里塞）
failures = []

# 「零增量」判据里的用户数据文件（%APPDATA% 由 env 决定 ⇒ 天然可隔离）
HISTORY_REL = os.path.join("HP_Scan_Tool", "history.json")


def _out(*a):
    """统一输出（Windows 控制台也可能不是 UTF-8）"""
    try:
        print(*a, flush=True)
    except UnicodeEncodeError:
        print(*[str(x).encode("ascii", "replace").decode("ascii") for x in a],
              flush=True)


def run_pytest(args, label):
    """跑一轮 pytest，返回 (是否通过, 末行摘要, 用时秒)"""
    t0 = time.time()
    r = subprocess.run([PY, "-m", "pytest"] + args + ["-q"],
                       cwd=ROOT, env=ENV, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    tail = [l for l in out.strip().splitlines() if l.strip()]
    summary = tail[-1] if tail else "(无输出)"
    dt = time.time() - t0
    _out(f"  [{label}] rc={r.returncode}  {dt:.1f}s  {summary}")
    if r.returncode != 0:
        failures.append(f"{label} rc={r.returncode}")
        for l in tail[-25:]:
            _out(f"      | {l}")
    return r.returncode == 0, summary, dt


def temp_hp_dirs():
    """%TEMP% 下本项目的临时目录（`hp_` 前缀）"""
    t = os.environ.get("TEMP") or os.environ.get("TMP") or ""
    try:
        return {d for d in os.listdir(t)
                if d.lower().startswith("hp_")
                and os.path.isdir(os.path.join(t, d))}
    except OSError:
        return set()


def explorer_windows():
    """explorer.exe 进程数（用户屏幕上开着多少个资源管理器窗口）"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/NH"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return sum(1 for l in (r.stdout or "").splitlines()
                   if "explorer.exe" in l.lower())
    except OSError:
        return -1


def user_history_sha():
    """真实用户扫描历史的 SHA256（不存在则 None）"""
    appdata = os.environ.get("APPDATA") or ""
    p = os.path.join(appdata, HISTORY_REL)
    if not os.path.isfile(p):
        return None
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest().upper()


def test_files():
    """tests/ 下的 test_*.py（按名字排序 ⇒ 种子可复现）"""
    return [os.path.join("tests", n) for n in sorted(os.listdir(TESTS))
            if n.startswith("test_") and n.endswith(".py")]


def main():
    ap = argparse.ArgumentParser(description="HP Scan Tool 交付门禁")
    ap.add_argument("--rounds", type=int, default=3, help="全量 pytest 轮数")
    ap.add_argument("--phase3", type=int, default=5, help="随机排列轮数")
    ap.add_argument("--seed", type=int, default=20261021, help="随机排列种子")
    ap.add_argument("--skip-phase3", action="store_true", help="跳过随机排列")
    args = ap.parse_args()

    files = test_files()
    _out("=" * 64)
    _out("  HP Scan Tool — 交付门禁")
    _out("=" * 64)
    _out(f"测试文件: {len(files)} 个")
    _out(f"Python: {PY}")

    # ── ① 全量 ×N ──
    _out(f"\n=== ① 全量 ×{args.rounds} ===")
    full_times = []
    for i in range(1, args.rounds + 1):
        ok, _s, dt = run_pytest(["tests"], f"全量 #{i}")
        full_times.append(dt)

    # ── ② Phase 3 随机排列 ×M ──
    phase3_times = []
    if not args.skip_phase3 and args.phase3 > 0:
        _out(f"\n=== ② Phase 3 随机排列 ×{args.phase3}（seed={args.seed}） ===")
        rng = random.Random(args.seed)
        for i in range(1, args.phase3 + 1):
            order = files[:]
            rng.shuffle(order)
            ok, _s, dt = run_pytest(order, f"Phase3 #{i}")
            phase3_times.append(dt)

    # ── ③ 零增量判据（跑前跑后对比，必须包住一次真实测试）──
    _out("\n=== ③ 零增量判据 ===")
    hp_before = temp_hp_dirs()
    exp_before = explorer_windows()
    hist_before = user_history_sha()
    _out(f"  跑前: hp_* 目录 {len(hp_before)} 个 | explorer {exp_before} 个 "
         f"| history {hist_before}")

    run_pytest(["tests"], "增量观察轮")

    hp_after = temp_hp_dirs()
    exp_after = explorer_windows()
    hist_after = user_history_sha()
    delta_hp = sorted(hp_after - hp_before)
    delta_exp = exp_after - exp_before
    _out(f"\n  a) %TEMP% hp_* 目录增量: {len(delta_hp)}  "
         f"{delta_hp if delta_hp else ''}")
    _out(f"  b) explorer 进程增量: {delta_exp}（{exp_before} → {exp_after}）")
    _out(f"  c) 用户 history.json SHA256: {hist_before} → {hist_after}  "
         f"{'不变' if hist_before == hist_after else '★ 变了'}")
    if delta_hp:
        failures.append(f"%TEMP% hp_* 增量 {len(delta_hp)}")
    if delta_exp > 0:
        failures.append(f"explorer 增量 {delta_exp}")
    if hist_before != hist_after:
        failures.append("用户 history.json 被改动")

    _out("\n" + "=" * 64)
    if failures:
        _out("★ 门禁未通过:")
        for f in failures:
            _out(f"   - {f}")
        return 1
    _out("★★ 门禁全部通过")
    if full_times:
        _out(f"   全量 ×{len(full_times)}: "
             + " / ".join(f"{t:.1f}s" for t in full_times))
    if phase3_times:
        _out(f"   Phase3 ×{len(phase3_times)}: "
             + " / ".join(f"{t:.1f}s" for t in phase3_times))
    _out("   零增量: hp_* 增量 0 / explorer 增量 0 / 用户数据不变")
    return 0


if __name__ == "__main__":
    sys.exit(main())
