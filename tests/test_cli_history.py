"""
CLI `history` 子命令**命令体**测试 — HP Scan Tool v4.0
覆盖链号 15.9：`cmd_history` **无任何用例调用过**
（`tests/test_cli.py` 里的 `TestHistoryArgs` 只测 argparse，不碰命令体）。

被守护的行为（源码事实，cli.py:349-371）
-----------------------------------------
    def cmd_history(args):
        limit = args.limit
        fmt = args.format
        records = history_manager.list_records(limit=limit)
        if fmt == "json":
            print(json.dumps(records, ensure_ascii=False, indent=2))
        else:
            if not records:
                print("无扫描历史")
            else:
                print(f"最近 {len(records)} 条扫描记录:")
                for i, r in enumerate(records, 1):
                    ts = r.get("timestamp", "")[:19].replace("T", " ")
                    device = r.get("device_name", r.get("device_ip", "?"))
                    pages = r.get("page_count", 1)
                    fmt_val = r.get("format", "?").upper()
                    path = r.get("file_path", "")
                    print(f"  {i}. [{ts}] {device} | {pages}页 | {fmt_val} | {path}")
        return 0

⇒ ① `--limit` 真的被用上（条数受限；默认 20）
  ② `--format json` 时 stdout 是**纯 JSON**：能被 json.loads 解析，
     且**不含任何非 JSON 的前后缀**（标题行 / 进度提示混进 stdout 就是坏了）
  ③ 读历史**不写**历史（命令自身不产生新记录）

★ 用户数据零污染：历史写进 conftest 的 APPDATA 沙箱
  （conftest.py 的 `_isolate_user_data` 已把 APPDATA 与
  `history_manager._app_dir` / `history_manager.HISTORY_FILE` 全部指向 tmp_path），
  真实用户的 `%APPDATA%\\HP_Scan_Tool\\history.json` 不会被碰。
"""

import json
import os

import pytest

import cli
import history_manager


# ── 造历史（假数据）────────────────────────────────────────

def _record(idx):
    """第 idx 条假记录（idx 从 1 开始，时间递增 ⇒ idx 越大越新）。"""
    return {
        "timestamp": "2026-10-21T0%d:00:00" % idx,
        "device_name": "HP M233",
        "device_ip": "192.168.7.%d" % idx,
        "page_count": idx,
        "format": "pdf",
        "file_path": os.path.join("C:\\out", "seed_%d.pdf" % idx),
        "source": "test",
    }


def _seed(count):
    """写入 count 条假历史，返回写入的记录列表（按写入顺序 = 最旧→最新）。"""
    records = [_record(i + 1) for i in range(count)]
    for rec in records:
        assert history_manager.append(rec) is True, "历史写入失败（沙箱不可写？）"
    return records


def _args(*argv):
    return cli._build_parser().parse_args(["history"] + list(argv))


# ── 条数语义 ───────────────────────────────────────────────

class TestCmdHistoryLimits:
    """--limit 是否真的限制了显示条数。"""

    def test_default_limit_lists_all_seeded_records(self, capsys):
        """默认 limit=20；只有 3 条 ⇒ 全部列出，标题里的条数是 3。"""
        _seed(3)

        rc = cli.cmd_history(_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "最近 3 条扫描记录:" in out, (
            "默认条数应显示全部 3 条，实际输出：%r" % out)
        for i in (1, 2, 3):
            assert "seed_%d.pdf" % i in out, "缺少第 %d 条记录：%r" % (i, out)

    def test_default_limit_truncates_to_twenty(self, capsys):
        """★ 默认 limit=20 真的生效：25 条记录只显示 20 条（不是全显示）。"""
        _seed(25)

        rc = cli.cmd_history(_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "最近 20 条扫描记录:" in out, (
            "默认应截断到 20 条，实际输出：%r" % out[:400])
        # 最新的一条在、倒数第 21 条不在
        assert "seed_25.pdf" in out
        assert "seed_5.pdf" not in out, "不应显示第 5 条（超出默认 20 条窗口）"

    def test_limit_one_lists_only_the_newest_record(self, capsys):
        """--limit 1 ⇒ 只显示**最新**那条（list_records 是最新在前）。"""
        _seed(3)

        rc = cli.cmd_history(_args("--limit", "1"))

        assert rc == 0
        out = capsys.readouterr().out
        assert "最近 1 条扫描记录:" in out, "实际输出：%r" % out
        assert "seed_3.pdf" in out, "应显示最新一条 seed_3.pdf"
        assert "seed_1.pdf" not in out and "seed_2.pdf" not in out, (
            "--limit 1 时不应出现更旧的记录：%r" % out)

    def test_limit_two_lists_two_newest_records(self, capsys):
        """--limit 2 ⇒ 两条，且是**最新两条**（seed_3 / seed_2）。"""
        _seed(3)

        rc = cli.cmd_history(_args("--limit", "2"))

        assert rc == 0
        out = capsys.readouterr().out
        assert "最近 2 条扫描记录:" in out, "实际输出：%r" % out
        assert "seed_3.pdf" in out and "seed_2.pdf" in out
        assert "seed_1.pdf" not in out

    def test_empty_history_prints_placeholder(self, capsys):
        """没有历史 ⇒ 打印"无扫描历史"，退出码 0，不抛。"""
        rc = cli.cmd_history(_args())

        assert rc == 0
        assert "无扫描历史" in capsys.readouterr().out


# ── --format json：stdout 必须是纯 JSON ────────────────────

class TestCmdHistoryJsonOutput:
    """★ --format json 时 stdout 只能是一个 JSON 文档，前后不许有杂物。"""

    def test_json_output_is_pure_json(self, capsys):
        """能整段 json.loads 解析 ⇒ 没有前缀/后缀噪音。"""
        _seed(3)

        rc = cli.cmd_history(_args("--format", "json"))

        assert rc == 0
        out = capsys.readouterr().out
        assert out.strip(), "stdout 不应为空"
        # 整段可解析（json.loads 对前后缀垃圾都会抛）
        data = json.loads(out)
        assert isinstance(data, list), "JSON 顶层必须是数组，实际 %r" % type(data)
        assert len(data) == 3, "应有 3 条记录，实际 %d" % len(data)

    def test_json_output_has_no_prefix_or_suffix(self, capsys):
        """显式检查：去掉空白后，JSON 文档必须占满整段 stdout。"""
        _seed(3)

        cli.cmd_history(_args("--format", "json"))

        out = capsys.readouterr().out
        stripped = out.strip()
        assert stripped.startswith("["), (
            "stdout 必须以 JSON 数组开头，实际首字符 %r" % stripped[:40])
        assert stripped.endswith("]"), (
            "stdout 必须以 JSON 数组结尾，实际末字符 %r" % stripped[-40:])
        # raw_decode：解析完一个 JSON 文档后，剩下的必须只有空白
        obj, end = json.JSONDecoder().raw_decode(stripped)
        assert stripped[end:].strip() == "", (
            "stdout 尾部存在非 JSON 后缀：%r" % stripped[end:])
        # 人类可读的标题行不许混进 JSON 输出
        assert "最近" not in out, "stdout 混进了人类可读标题：%r" % out[:200]
        assert "条扫描记录" not in out

    def test_json_content_matches_seeded_records(self, capsys):
        """JSON 里的字段与内容必须与写入的记录一致（最新在前）。"""
        seeded = _seed(3)

        cli.cmd_history(_args("--format", "json"))

        data = json.loads(capsys.readouterr().out)
        got_paths = [r.get("file_path") for r in data]
        assert got_paths == [r["file_path"] for r in reversed(seeded)], (
            "JSON 记录顺序/内容不对：%r" % (got_paths,))
        assert data[0].get("device_name") == "HP M233"
        assert data[0].get("format") == "pdf"

    def test_json_output_with_limit_one_is_pure_json(self, capsys):
        """--limit 1 --format json ⇒ 仍是纯 JSON，且只有 1 条。"""
        _seed(3)

        rc = cli.cmd_history(_args("--limit", "1", "--format", "json"))

        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 1, "应有 1 条记录，实际 %d" % len(data)
        assert data[0].get("file_path", "").endswith("seed_3.pdf")

    def test_json_output_with_empty_history_is_pure_json(self, capsys):
        """没有历史时，JSON 模式输出必须是合法的空数组（不是空串 / 提示语）。"""
        rc = cli.cmd_history(_args("--format", "json"))

        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out) == [], "空历史应输出 []，实际 %r" % out


# ── 只读性 ─────────────────────────────────────────────────

class TestCmdHistoryDoesNotWrite:
    """读历史不该写历史（命令自身不产生新记录）。"""

    def test_reading_history_leaves_file_untouched(self, capsys):
        _seed(3)
        path = history_manager._history_file()
        with open(path, "rb") as fh:
            before = fh.read()

        cli.cmd_history(_args("--format", "json"))
        cli.cmd_history(_args())

        with open(path, "rb") as fh:
            after = fh.read()
        assert after == before, "读历史改写了历史文件（不该发生）"
        assert len(history_manager.list_records(limit=999)) == 3
