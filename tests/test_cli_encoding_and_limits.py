# -*- coding: utf-8 -*-
"""
CLI 的 **JSON 编码契约** + **`--limit` 条数语义** + **`%f` 单位注释** — HP Scan Tool v4.0

被守护的行为（全部来自终审报告
`.superpowers/sdd/2026-10-20_扫描流程统一到协调器_实施计划_v1.0/final-review-save-session-gui-cli.md`）
--------------------------------------------------------------------------------------
① 【复审 I-3 · Important】`--format json` 的 stdout 必须是**纯 ASCII 字节**。

   根因：Windows 上 `sys.stdout` 用 **locale 编码**（本机 zh-CN ⇒ cp936/GBK）；
   stdout 被**管道 / 重定向到文件**时，含中文的 JSON 落成 **GBK 字节**
   ⇒ 消费方按 UTF-8 读（`json.loads(stdout_bytes)` / `jq` /
   `Get-Content -Encoding utf8`）直接 UnicodeDecodeError
   ⇒ RFC 8259 规定 JSON 文本用 UTF-8 ⇒ 这类消费方式在本机**天天都会炸**。

   契约：`--format json` 的输出**纯 ASCII**（非 ASCII 转成 `\\uXXXX` 转义）
        ⇒ GBK / UTF-8 / Latin-1 下**字节完全一致**、都能 `json.loads`。

② 【复审 · 真 bug】`--limit 0` 必须显示 **0 条**（不是全部）。

   根因：`history_manager.list_records` 用 `records[-limit:]`
   ⇒ `limit=0` 时 `records[-0:]` 等于**全量** ⇒ `--limit 0` 打印全部记录。

③ `--limit` 为负数必须**明确报错**：`records[--1:]` 是合法 Python
   ⇒ 语义诡异（返回「除最旧一条外的全部」）⇒ 不允许静默。

④ 【复审 M-3 → 2026-10-21 统一形态】产物名里的时间戳**只到秒**，
   且**不许**把精度说高（既不许写「毫秒」，也不许留一个同 tick 内
   必然重复的微秒段 —— 那是**虚假精度**）。

★ 用户数据零污染
   - 进程内用例：走 `tests/conftest.py` 的 APPDATA 沙箱（`_isolate_user_data`）；
   - 子进程用例：**自己**把 APPDATA 指到 `tmp_path` 沙箱，
     真实 `%APPDATA%\\HP_Scan_Tool\\history.json` 不会被碰。

★ 不发任何真实网络请求
   - `discover` / `status` 全部**进程内** monkeypatch 掉网络函数；
   - 子进程**只跑 `history`**（只读沙箱历史文件，不触网）。

★ 本文件**只**守护上面四条，不复制其它文件已有的用例。
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

import cli

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── 假数据（含中文 ⇒ 正是能把「GBK 字节」打出来的那种内容）────────────
DEVICE_NAME = "HP LaserJet M233sdw 中文复印机"
MODEL_NAME = "中文型号 M233"
FILE_STEM = "扫描件"
ERROR_TEXT = "设备没响应"


class _FakeScanner:
    """假扫描仪（字段与 escl_engine.ScannerInfo 对齐）。"""

    name = DEVICE_NAME
    ip = "192.168.7.31"
    model = MODEL_NAME
    escl_url = "http://192.168.7.31/eSCL/"
    has_adf = True
    has_duplex = True
    resolutions = [300, 600]
    color_modes = ["RGB24", "Grayscale8"]
    formats = ["pdf", "jpeg"]

    @property
    def display_name(self):
        return "%s (%s)" % (self.name, self.ip)


def _args(*argv):
    """用真实 parser 造 args（走 argparse 的校验路径）。"""
    return cli._build_parser().parse_args(list(argv))


# ── 沙箱历史 ───────────────────────────────────────────────────────────

def _seed_history_file(appdata, count=3):
    """在沙箱 APPDATA 里写一份含中文的 history.json（UTF-8）。

    返回写入的记录列表（写入顺序 = 最旧 → 最新）。
    """
    hist_dir = pathlib.Path(appdata) / "HP_Scan_Tool"
    hist_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(count):
        records.append({
            "timestamp": "2026-10-21T09:%02d:00" % i,
            "device_name": DEVICE_NAME,
            "device_ip": "192.168.7.31",
            "page_count": i + 1,
            "format": "pdf",
            "file_path": "C:\\out\\%s_%03d.pdf" % (FILE_STEM, i + 1),
            "source": "cli",
        })
    (hist_dir / "history.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return records


def _seed_in_process(count=3):
    """进程内写历史（落到 conftest 的 APPDATA 沙箱，不碰真实用户数据）。"""
    import history_manager
    for i in range(count):
        rec = {
            "device_name": DEVICE_NAME,
            "device_ip": "192.168.7.31",
            "page_count": i + 1,
            "format": "pdf",
            "file_path": "C:\\out\\seed_%03d.pdf" % (i + 1),
            "source": "cli",
        }
        assert history_manager.append(rec) is True, "沙箱历史写入失败"
    return count


# ── 子进程（只跑 history：不触网）─────────────────────────────────────

def _run_cli(argv, appdata, ioencoding=None, timeout=120):
    """拉起真实 CLI 子进程，返回 CompletedProcess（stdout/stderr 是**原始字节**）。

    ★ 必须**清掉** PYTHONIOENCODING / PYTHONUTF8：
      否则子进程按 UTF-8 输出，恰好掩盖住「控制台代码页（GBK）」这个 bug。
    """
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)          # 用户数据隔离
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONIOENCODING", None)
    env.pop("PYTHONUTF8", None)
    if ioencoding:
        env["PYTHONIOENCODING"] = ioencoding
    return subprocess.run(
        [sys.executable, "-m", "cli"] + list(argv),
        cwd=str(PROJECT_ROOT), env=env,
        capture_output=True, timeout=timeout,
    )


# ══════════════════════════════════════════════════════════════════════
# ① JSON 输出必须是纯 ASCII（复审 I-3）
# ══════════════════════════════════════════════════════════════════════

class TestJsonOutputIsPureAscii:
    """★ 核心断言：`--format json` 的 stdout 字节是纯 ASCII，任意代码页都能 parse。"""

    def test_history_json_subprocess_bytes_parse_as_json(self, tmp_path):
        """★ 消费方视角：`json.loads(stdout_bytes)` 必须成功（不再 UnicodeDecodeError）。"""
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=3)

        proc = _run_cli(["history", "--format", "json"], appdata)

        assert proc.returncode == 0, (
            "CLI 退出码 %d，stderr=%r" % (proc.returncode, proc.stderr[:2000]))
        # 纯 ASCII：GBK / UTF-8 / Latin-1 下字节完全一致
        assert proc.stdout.decode("ascii") is not None, (
            "stdout 不是纯 ASCII（含非 ASCII 字节）⇒ 按 UTF-8 读的消费方会崩")
        data = json.loads(proc.stdout)      # 字节直接 parse
        assert isinstance(data, list)
        assert len(data) == 3, "应有 3 条记录，实际 %d" % len(data)
        # 转义后仍能还原成原文（JSON 转义不丢信息）
        assert data[0]["device_name"] == DEVICE_NAME
        assert data[0]["file_path"].endswith("%s_003.pdf" % FILE_STEM)

    def test_history_json_bytes_identical_across_codepages(self, tmp_path):
        """★ 同一份输出在 GBK / UTF-8 / Latin-1 三种代码页下**字节完全一致**。"""
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=2)

        raw_by_codepage = {}
        for enc in (None, "cp936", "utf-8", "latin-1"):
            proc = _run_cli(["history", "--format", "json"], appdata, ioencoding=enc)
            assert proc.returncode == 0, "代码页 %r 下退出码 %d" % (enc, proc.returncode)
            raw_by_codepage[enc] = proc.stdout
            # 每种代码页下都必须能按 ASCII 读、都能 parse
            assert proc.stdout.decode("ascii") is not None
            assert len(json.loads(proc.stdout)) == 2

        assert len(set(raw_by_codepage.values())) == 1, (
            "不同代码页下的 JSON 输出字节不一致（说明输出不是纯 ASCII）：%r"
            % {k: v[:80] for k, v in raw_by_codepage.items()})

    def test_discover_json_output_is_ascii(self, monkeypatch, capsys):
        """discover 的 JSON 分支：设备名是中文 ⇒ 输出必须是纯 ASCII。"""
        monkeypatch.setattr(cli, "discover_all_scanners",
                            lambda timeout=4.0: [_FakeScanner()])

        rc = cli.cmd_discover(_args("discover", "--format", "json"))

        assert rc == 0
        out = capsys.readouterr().out
        assert out.isascii(), "discover 的 JSON 输出含非 ASCII 字符：%r" % out[:200]
        data = json.loads(out)
        assert data[0]["name"] == DEVICE_NAME
        assert data[0]["model"] == MODEL_NAME

    def test_status_json_output_is_ascii(self, monkeypatch, capsys):
        """status 在线分支：状态字段是中文 ⇒ 输出必须是纯 ASCII。"""
        monkeypatch.setattr(cli, "probe_escl",
                            lambda ip, **kw: "http://192.168.7.31/eSCL/")
        monkeypatch.setattr(cli, "get_scanner_status",
                            lambda url, **kw: {"state": "处理中", "adf_loaded": True})

        rc = cli.cmd_status(_args("status", "--ip", "192.168.7.31", "--format", "json"))

        assert rc == 0
        out = capsys.readouterr().out
        assert out.isascii(), "status 的 JSON 输出含非 ASCII 字符：%r" % out[:200]
        data = json.loads(out)
        assert data["state"] == "处理中"
        assert data["ip"] == "192.168.7.31"

    def test_status_error_json_output_is_ascii(self, monkeypatch, capsys):
        """status 报错分支：错误信息是中文 ⇒ 输出必须是纯 ASCII 且能还原。"""
        monkeypatch.setattr(cli, "probe_escl",
                            lambda ip, **kw: "http://192.168.7.31/eSCL/")

        def _boom(url, **kw):
            raise RuntimeError(ERROR_TEXT)

        monkeypatch.setattr(cli, "get_scanner_status", _boom)

        rc = cli.cmd_status(_args("status", "--ip", "192.168.7.31", "--format", "json"))

        assert rc == 1
        out = capsys.readouterr().out
        assert out.isascii(), "status 错误分支的 JSON 输出含非 ASCII：%r" % out[:200]
        assert json.loads(out)["error"] == ERROR_TEXT

    def test_status_offline_json_output_is_pure_json(self, monkeypatch, capsys):
        """status 离线分支：stdout 仍是**纯 JSON**（可 parse）。"""
        monkeypatch.setattr(cli, "probe_escl", lambda ip, **kw: None)

        rc = cli.cmd_status(_args("status", "--ip", "192.168.7.31", "--format", "json"))

        assert rc == 1
        out = capsys.readouterr().out
        assert out.isascii()
        assert json.loads(out)["status"] == "offline"

    def test_all_json_output_goes_through_one_dumps(self):
        """★ 所有 `--format json` 的输出必须走**同一个** dumps（口径不许分叉）。"""
        src = (PROJECT_ROOT / "cli.py").read_text(encoding="utf-8")
        calls = [(i, line.strip()) for i, line in enumerate(src.splitlines(), 1)
                 if re.search(r"json\.dumps\(", line)]
        assert len(calls) == 1, (
            "cli.py 里必须只有一处 json.dumps 调用（统一出口），实际 %d 处：%r"
            % (len(calls), calls))
        assert "ensure_ascii=True" in calls[0][1], (
            "统一出口必须显式 ensure_ascii=True（输出纯 ASCII），实际：%r" % calls[0][1])


# ══════════════════════════════════════════════════════════════════════
# ② --limit 0 ⇒ 0 条
# ══════════════════════════════════════════════════════════════════════

class TestHistoryLimitZero:
    """★ `--limit 0` 必须显示 **0 条**（`records[-0:]` 是全量 ⇒ 直接透传是 bug）。"""

    def test_limit_zero_subprocess_json_is_empty_list(self, tmp_path):
        """子进程视角：`--limit 0 --format json` ⇒ `[]`。"""
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=3)

        proc = _run_cli(["history", "--format", "json", "--limit", "0"], appdata)

        assert proc.returncode == 0, (
            "退出码 %d，stderr=%r" % (proc.returncode, proc.stderr[:2000]))
        assert json.loads(proc.stdout) == [], (
            "--limit 0 应输出空数组，实际 %r" % proc.stdout[:300])

    def test_limit_zero_subprocess_text_prints_placeholder(self, tmp_path):
        """子进程视角：`--limit 0`（文本）⇒ 打印「无扫描历史」，不列出任何记录。"""
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=3)

        proc = _run_cli(["history", "--limit", "0"], appdata, ioencoding="utf-8")

        assert proc.returncode == 0
        out = proc.stdout.decode("utf-8")
        assert "无扫描历史" in out, "实际输出：%r" % out
        assert FILE_STEM not in out, "--limit 0 不该列出任何记录：%r" % out

    def test_limit_zero_in_process_shows_nothing(self, capsys):
        """进程内视角：`cmd_history(--limit 0)` ⇒ 0 条。"""
        _seed_in_process(3)

        rc = cli.cmd_history(_args("history", "--limit", "0"))

        assert rc == 0
        out = capsys.readouterr().out
        assert "无扫描历史" in out, "实际输出：%r" % out
        assert "seed_001.pdf" not in out, "--limit 0 不该列出任何记录：%r" % out

    def test_limit_zero_in_process_json_is_empty_list(self, capsys):
        """进程内视角：`--limit 0 --format json` ⇒ `[]`。"""
        _seed_in_process(3)

        rc = cli.cmd_history(_args("history", "--limit", "0", "--format", "json"))

        assert rc == 0
        assert json.loads(capsys.readouterr().out) == []


# ══════════════════════════════════════════════════════════════════════
# ③ --limit 负数 ⇒ 明确报错
# ══════════════════════════════════════════════════════════════════════

class TestHistoryLimitNegative:
    """★ 负数必须**明确报错**（`records[--1:]` 语义诡异，不允许静默）。"""

    def test_parser_rejects_negative_limit(self, capsys):
        """argparse 层：负数 ⇒ SystemExit(2) + stderr 提示。"""
        with pytest.raises(SystemExit) as excinfo:
            cli._build_parser().parse_args(["history", "--limit", "-1"])

        assert excinfo.value.code == 2, "用法错误必须退出码 2"
        err = capsys.readouterr().err
        assert "--limit" in err, "stderr 必须点名 --limit：%r" % err
        assert "负" in err, "stderr 必须说清「负数」不可用：%r" % err

    def test_command_body_rejects_negative_limit(self, capsys):
        """★ 命令体自身也要挡（绕过 argparse 直接调用的路径）。"""
        args = argparse.Namespace(command="history", limit=-1, format="json")

        rc = cli.cmd_history(args)

        assert rc != 0, "负数 limit 必须返回非 0"
        captured = capsys.readouterr()
        assert captured.out == "", "失败时 stdout 必须为空，实际 %r" % captured.out
        assert "负" in captured.err, "stderr 必须说清「负数」不可用：%r" % captured.err

    def test_negative_limit_subprocess_exits_nonzero(self, tmp_path):
        """子进程视角：负数 ⇒ 退出码非 0、stdout 干净、stderr 有提示。"""
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=3)

        proc = _run_cli(["history", "--format", "json", "--limit=-1"],
                        appdata, ioencoding="utf-8")

        assert proc.returncode != 0, "负数 limit 必须失败退出"
        assert proc.stdout == b"", "失败时 stdout 必须为空，实际 %r" % proc.stdout[:200]
        err = proc.stderr.decode("utf-8", errors="replace")
        assert "负" in err, "stderr 必须说清「负数」不可用：%r" % err


# ══════════════════════════════════════════════════════════════════════
# ④ --limit 正数语义不许被改坏（回归护栏）
# ══════════════════════════════════════════════════════════════════════

class TestHistoryLimitPositiveStillWorks:
    """修 0 的同时不许把正常条数语义改坏。"""

    def test_limit_two_lists_two_newest(self, tmp_path):
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=3)

        proc = _run_cli(["history", "--format", "json", "--limit", "2"], appdata)

        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert len(data) == 2, "应有 2 条，实际 %d" % len(data)
        # 最新在前
        assert data[0]["file_path"].endswith("%s_003.pdf" % FILE_STEM)
        assert data[1]["file_path"].endswith("%s_002.pdf" % FILE_STEM)

    def test_limit_one_lists_newest_only(self, tmp_path):
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=3)

        proc = _run_cli(["history", "--format", "json", "--limit", "1"], appdata)

        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert len(data) == 1
        assert data[0]["file_path"].endswith("%s_003.pdf" % FILE_STEM)

    def test_default_limit_still_twenty(self, tmp_path):
        appdata = tmp_path / "appdata"
        _seed_history_file(appdata, count=25)

        proc = _run_cli(["history", "--format", "json"], appdata)

        assert proc.returncode == 0
        assert len(json.loads(proc.stdout)) == 20


# ══════════════════════════════════════════════════════════════════════
# ⑤ %f 是微秒，不是毫秒（复审 M-3）
# ══════════════════════════════════════════════════════════════════════

class TestTimestampPrecisionIsMicroseconds:
    """★ 时间戳的精度描述必须与**实际形态**一致。

    ★ 2026-10-21 统一形态：产物名里的时间戳**只到秒**
      （``{YYYYmmdd}_{HHMMSS}``）—— 微秒段**已下线**。

      为什么下线：本机 Windows **系统时钟粒度实测 15.625 ms**
      ⇒ 同一 tick 内两次 ``strftime("%f")`` 的结果 **99.5% 完全相同**
      ⇒ 那 6 位微秒**没有额外信息量** ⇒ 保留它是**虚假精度**
      （会让人误以为产物名精确到微秒）。

      ⇒ 本类的意图从「`%f` 是微秒不是毫秒」**升级**为
        「**不许**把精度说高 / 做高」：既不许写「毫秒」，
        也不许在名字里留一个同 tick 内必然重复的微秒段。
    """

    def test_cli_py_never_claims_milliseconds(self):
        """cli.py 里不许再出现「毫秒」的说法（该文件唯一的时间精度讨论就是 `%f`）。"""
        src = (PROJECT_ROOT / "cli.py").read_text(encoding="utf-8")
        offenders = [(i, line.strip()) for i, line in enumerate(src.splitlines(), 1)
                     if "毫秒" in line]
        assert offenders == [], (
            "cli.py 里 %%f 是**微秒**，不许写成「毫秒」：%r" % (offenders,))

    def test_timestamped_name_docstring_states_second_level_precision(self):
        """`_timestamped_name` 的 docstring 必须写明「秒级」且不写「毫秒」。"""
        doc = cli._timestamped_name.__doc__ or ""
        assert "毫秒" not in doc, "docstring 把时间精度说成了毫秒：%r" % doc
        assert "秒级" in doc, "docstring 必须写明「秒级」：%r" % doc

    def test_generated_name_has_no_microsecond_segment(self):
        """事实锚点：产物名里**没有**微秒段（基名只有三段）。

        ★ 2026-10-21：契约定为
          `{前缀}_{YYYYmmdd}_{HHMMSS}_{1 位字母 + 7 位十六进制}.{ext}`
          ⇒ 基名从**四段**变成**三段**（微秒段下线）。
        ★ 意图不变（而且更强）：时间戳的精度描述必须**与名字里实际的段数一致**
          —— 名字里写多细，就必须真有那么多信息量。
        """
        name = cli._timestamped_name("HP_CLI", "pdf")
        stem = name[len("HP_CLI_"):-len(".pdf")]
        parts = stem.split("_")
        assert len(parts) == 3, (
            "应为「日期_时分秒_唯一成分」三段（不许再有微秒段），实际 %r" % (name,))
        assert parts[1].isdigit() and len(parts[1]) == 6, (
            "第二段必须是 6 位时分秒：%r" % name)
        assert re.fullmatch(r"[a-p][0-9a-f]{7}", parts[2]), (
            "第三段必须是「1 位字母 + 7 位十六进制」的随机唯一成分：%r" % name)
