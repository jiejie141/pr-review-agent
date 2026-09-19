"""diff 解析测试。

这一层的正确性是整个项目的地基：行号错了，后面所有意见都会挂到错误的代码行上。
"""

from pagent.diffparse import index_added_lines, parse_diff_stats, parse_unified_diff
from pagent.models import LineKind

GIT_FULL = """diff --git a/app/main.py b/app/main.py
index 1a2b3c4..5d6e7f8 100644
--- a/app/main.py
+++ b/app/main.py
@@ -1,4 +1,5 @@
 import os
 
 def main():
-    pass
+    print("hello")
+    return 0
"""


def test_parse_git_full_header():
    files = parse_unified_diff(GIT_FULL)
    assert len(files) == 1
    f = files[0]
    assert f.path == "app/main.py"
    assert f.old_path == "app/main.py"
    assert not f.is_new
    assert not f.is_deleted
    assert len(f.hunks) == 1


def test_added_line_numbers_are_new_file_numbers():
    f = parse_unified_diff(GIT_FULL)[0]
    added = [(ln.new_no, ln.text) for ln in f.added_lines()]
    # 三行上下文占 1~3，新增行落在第 4、5 行
    assert added == [
        (4, '    print("hello")'),
        (5, "    return 0"),
    ]


def test_removed_line_keeps_old_number():
    f = parse_unified_diff(GIT_FULL)[0]
    dels = [ln for ln in f.hunks[0].lines if ln.kind is LineKind.DEL]
    assert len(dels) == 1
    assert dels[0].old_no == 4
    assert dels[0].new_no is None


def test_context_lines_have_both_numbers():
    f = parse_unified_diff(GIT_FULL)[0]
    ctx = [ln for ln in f.hunks[0].lines if ln.kind is LineKind.CONTEXT]
    assert ctx[0].text == "import os"
    assert ctx[0].old_no == 1
    assert ctx[0].new_no == 1


def test_new_file_detection():
    patch = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/new.py
@@ -0,0 +1,3 @@
+line one
+line two
+line three
"""
    f = parse_unified_diff(patch)[0]
    assert f.is_new
    assert f.path == "new.py"
    assert [ln.new_no for ln in f.added_lines()] == [1, 2, 3]


def test_deleted_file_detection():
    patch = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-line one
-line two
"""
    f = parse_unified_diff(patch)[0]
    assert f.is_deleted
    assert f.path == "gone.py"
    assert f.added_line_count == 0


def test_multiple_files_are_separated():
    patch = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n x\n+x2\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,2 @@\n y\n+y2\n"
    )
    files = parse_unified_diff(patch)
    assert [f.path for f in files] == ["a.py", "b.py"]
    assert all(len(f.hunks) == 1 for f in files)


def test_bare_diff_deleted_line_looking_like_header():
    """关键用例：被删除的行若内容以 `-- ` 开头，展开后与文件头完全同形。

    必须按 hunk 头声明的行数消费才能区分，否则第二个文件的内容会被并进第一个文件。
    """
    patch = """--- a/one.txt
+++ b/one.txt
@@ -1,2 +1,2 @@
 keep
--- old text starting with dashes
+new text
--- a/two.txt
+++ b/two.txt
@@ -1,1 +1,2 @@
 a
+b
"""
    files = parse_unified_diff(patch)
    assert [f.path for f in files] == ["one.txt", "two.txt"]

    one = files[0]
    dels = [ln for ln in one.hunks[0].lines if ln.kind is LineKind.DEL]
    assert len(dels) == 1
    assert dels[0].text == "-- old text starting with dashes"

    assert files[1].added_line_count == 1


def test_no_newline_marker_does_not_shift_counts():
    patch = """--- a/f.py
+++ b/f.py
@@ -1,1 +1,1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""
    f = parse_unified_diff(patch)[0]
    assert f.added_line_count == 1
    added = list(f.added_lines())
    assert added[0].new_no == 1
    assert added[0].text == "new"


def test_binary_file_flagged():
    patch = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    f = parse_unified_diff(patch)[0]
    assert f.is_binary
    assert f.added_line_count == 0


def test_empty_input_returns_empty_list():
    assert parse_unified_diff("") == []
    assert parse_unified_diff("not a diff at all") == []


def test_hunk_header_without_length_defaults_to_one():
    patch = "--- a/f.py\n+++ b/f.py\n@@ -5 +5 @@\n-old\n+new\n"
    f = parse_unified_diff(patch)[0]
    h = f.hunks[0]
    assert h.old_start == 5 and h.old_len == 1
    assert h.new_start == 5 and h.new_len == 1


def test_context_window_includes_surrounding_lines():
    patch = (
        "--- a/f.py\n+++ b/f.py\n@@ -1,6 +1,7 @@\n"
        " a\n b\n c\n d\n+x\n e\n f\n"
    )
    f = parse_unified_diff(patch)[0]
    win = f.context_window(5, radius=2)
    assert "x" in win
    assert "c" in win and "e" in win
    assert "a" not in win


def test_context_window_returns_empty_for_missing_line():
    f = parse_unified_diff(GIT_FULL)[0]
    assert f.context_window(9999) == ""


def test_suffix_and_counts():
    patch = (
        "diff --git a/x/Handler.Java b/x/Handler.Java\n--- a/x/Handler.Java\n"
        "+++ b/x/Handler.Java\n@@ -1,2 +1,3 @@\n a\n+x\n+yy\n"
    )
    f = parse_unified_diff(patch)[0]
    assert f.suffix == ".java"
    assert f.added_line_count == 2

    patch2 = "--- a/Makefile\n+++ b/Makefile\n@@ -1,1 +1,2 @@\n a\n+b\n"
    assert parse_unified_diff(patch2)[0].suffix == ""


def test_index_added_lines_set():
    f = parse_unified_diff(GIT_FULL)
    idx = index_added_lines(f)
    assert ("app/main.py", 4) in idx
    assert ("app/main.py", 5) in idx
    assert ("app/main.py", 1) not in idx  # 上下文行不应在集合里


def test_parse_diff_stats():
    s = parse_diff_stats(GIT_FULL)
    assert s["files"] == 1
    assert s["added"] == 2
    assert s["removed"] == 1


def test_render_marks_line_numbers():
    f = parse_unified_diff(GIT_FULL)[0]
    text = f.render()
    assert "4: " in text
    assert "### 文件: app/main.py" in text


def test_render_truncates():
    f = parse_unified_diff(GIT_FULL)[0]
    assert "已截断" in f.render(max_lines=1)
