"""
GUI 原子写图的临时文件名必须**同进程内唯一** — 回归测试

★ 缺陷（终审 Minor 里被标为「低危」的一条，我复核后确认值得修）
--------------------------------------------------------------------------
`hp_scan_gui_pyside.py::_write_image_atomic` 用
``tmp = f"{path}.tmp-{os.getpid()}"`` 作临时文件名。

`os.getpid()` **同进程内所有线程相同** ⇒ 两个线程同时原子写**同一个目标**时
会共用同一个临时文件：

- 线程 A 正在往 tmp 写，线程 B 也往同一个 tmp 写 ⇒ 两者的字节**交错**；
- 谁先 `os.replace(tmp, path)`，谁就把**交错内容**变成目标文件
  ⇒ 目标是**半截/损坏**的图片；
- 且 `finally` 里的 `os.remove(tmp)` 会把**另一个线程正在写的** tmp 删掉
  ⇒ 对方 `os.replace` 抛 FileNotFoundError ⇒ 本次更新**丢失**。

同目录的 `config_io.atomic_write_json` 用的是 `tempfile.mkstemp`（安全），
这处没跟 ⇒ 属**同族的遗漏**。

⇒ 本文件钉住：临时文件名必须**同进程内唯一**（不同线程 ⇒ 不同名）。
"""

import os
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402

import hp_scan_gui_pyside as gui  # noqa: E402


@pytest.fixture(scope="module")
def qapp(qt_app):
    yield qt_app


def test_temp_name_differs_between_threads(tmp_path, qapp, monkeypatch):
    """★ 两个线程写同一目标 ⇒ 捕获到的临时文件名必须**不同**。"""
    target = str(tmp_path / "same_target.jpg")
    seen = []
    real_replace = os.replace

    def _spy(src, dst):
        seen.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", _spy)

    barrier = threading.Barrier(2)
    #: 竞态失败的异常（Windows 上两个线程 replace 到**同一目标** ⇒ 输的那一个
    #: 会拿到 PermissionError(WinError 5)「拒绝访问」，因为目标文件正被对方占用）
    errors = []

    def _writer(color):
        img = Image.new("RGB", (120, 160), color)
        barrier.wait()                       # 让两个线程真正重叠
        try:
            gui._write_image_atomic(img, target, "jpg")
        except PermissionError as e:
            # ★ 绝不能让它逃出线程：pytest 会把「线程里未处理的异常」记成
            #   PytestUnhandledThreadExceptionWarning ⇒ **污染整轮测试的告警基线**
            #   （本轮实测：平时那 1 条 warning 就是它；它还会盖住真正的告警）。
            # ★ 这里断言"只允许是 Windows 的目标占用冲突"，其它异常照旧抛出。
            errors.append(e)

    threads = [threading.Thread(target=_writer, args=(c,))
               for c in ("red", "blue")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "写图线程没结束"

    assert len(seen) == 2, "两次原子写应各产生一次 os.replace，实际 %d" % len(seen)
    assert seen[0] != seen[1], (
        "两个线程共用同一个临时文件名 ⇒ 会互相破坏、产出半截文件: %s" % seen)
    for err in errors:
        assert isinstance(err, PermissionError), (
            "竞态里出现的异常必须是 PermissionError（Windows 目标占用），实际 %r" % err)
    for name in seen:
        # 临时文件必须与目标**同目录**（os.replace 跨目录就不是原子替换了）
        assert os.path.dirname(os.path.abspath(name)) == \
            os.path.dirname(os.path.abspath(target)), (
            "临时文件必须与目标同目录: %s" % name)
        # 且名字要能看出它属于哪个目标
        assert os.path.basename(target) in os.path.basename(name), (
            "临时名应基于目标文件名: %s" % name)


def test_no_temp_file_left_behind(tmp_path, qapp):
    """★ 写完后目录里不能残留临时文件。"""
    target = str(tmp_path / "clean.jpg")
    img = Image.new("RGB", (80, 80), "green")
    gui._write_image_atomic(img, target, "jpg")
    leftovers = [p for p in os.listdir(str(tmp_path)) if ".tmp-" in p]
    assert leftovers == [], "残留临时文件: %s" % leftovers
    assert os.path.exists(target), "目标文件没落盘"
    with Image.open(target) as im:
        assert im.size == (80, 80), "目标文件尺寸不对（可能被写坏）"
