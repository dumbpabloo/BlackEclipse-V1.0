"""File System subsystem — the patch/edit engine.

Exact replacement, ambiguity, missing target, stale hash, context mismatch,
unified diff, multi-file atomic failure/rollback, dry-run, encoding/line-ending
preservation.
"""

from __future__ import annotations

import pytest

from harness.code.fs import errors as err

from fs_helpers import make_service, tree


def test_exact_replacement_touches_only_that_file(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    other_before = (root / "src" / "other.py").read_bytes()
    result = svc.replace_text("src/app.py", "beta", "BETA", expected_occurrences=1)
    assert result["changed"] is True
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"
    assert (root / "src" / "other.py").read_bytes() == other_before


def test_ambiguous_replacement_refused(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.replace_text("src/dup.txt", "same", "done")
    assert exc.value.code == err.AMBIGUOUS_PATCH
    assert exc.value.details["occurrences"] == 2
    # Nothing was changed.
    assert (root / "src" / "dup.txt").read_text(encoding="utf-8") == "same\nsame\n"


def test_expected_occurrences_mismatch_refused(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.replace_text("src/dup.txt", "same", "done", expected_occurrences=3)
    assert exc.value.code == err.AMBIGUOUS_PATCH


def test_count_replaces_many_when_explicit(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
    svc = make_service(root)
    result = svc.replace_text("src/dup.txt", "same", "done", count=2)
    assert result["replaced"] == 2
    assert (root / "src" / "dup.txt").read_text(encoding="utf-8") == "done\ndone\n"


def test_missing_target_refused(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.replace_text("src/app.py", "nope", "x")
    assert exc.value.code == err.PATCH_CONTEXT_MISMATCH


def test_stale_hash_refused(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.patch_file("src/app.py", "replace", old_text="beta", new_text="B", expected_hash="0" * 64)
    assert exc.value.code == err.STALE_FILE


def test_regex_replacement(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    svc.replace_text("src/app.py", r"\balpha\b", "ALPHA", regex=True, expected_occurrences=1)
    assert (root / "src" / "app.py").read_text(encoding="utf-8").startswith("ALPHA\n")


def test_insert_after_anchor(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    svc.insert_text("src/app.py", "inserted", after_anchor="beta")
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\nbeta\ninserted\ngamma\n"


def test_insert_ambiguous_anchor_refused(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "dup.txt").write_text("x\ny\n", encoding="utf-8")
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.insert_text("src/dup.txt", "z", anchor="")
    assert exc.value.code in (err.INVALID_PATH, err.AMBIGUOUS_PATCH)


def test_insert_ambiguous_anchor_with_text_refused(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "dup.txt").write_text("anchor\nmid\nanchor\n", encoding="utf-8")
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.insert_text("src/dup.txt", "z", anchor="anchor")
    assert exc.value.code == err.AMBIGUOUS_PATCH


def test_delete_text_by_span(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    svc.delete_text("src/app.py", old_text="beta\n")
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_delete_text_by_line_range(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    svc.delete_text("src/app.py", start_line=2, end_line=2)
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\ngamma\n"


def test_apply_unified_diff(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    diff = (
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n"
    )
    result = svc.apply_diff(diff)
    assert result["filesChanged"] == 1
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\nBETA\ngamma\n"


def test_apply_diff_context_mismatch_refused(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    diff = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n WRONG\n-beta\n+BETA\n gamma\n"
    with pytest.raises(err.FsError) as exc:
        svc.apply_diff(diff)
    assert exc.value.code == err.PATCH_CONTEXT_MISMATCH
    assert (root / "src" / "app.py").read_text(encoding="utf-8") == "alpha\nbeta\ngamma\n"


def test_apply_diff_is_atomic_across_files(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    app_before = (root / "src" / "app.py").read_bytes()
    diff = (
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n"
        "--- a/src/other.py\n+++ b/src/other.py\n@@ -1,1 +1,1 @@\n-NOPE\n+X\n"
    )
    with pytest.raises(err.FsError) as exc:
        svc.apply_diff(diff)
    assert exc.value.code == err.PATCH_CONTEXT_MISMATCH
    assert (root / "src" / "app.py").read_bytes() == app_before


def test_multi_file_patch_atomic_failure_applies_nothing(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    app_before = (root / "src" / "app.py").read_bytes()
    other_before = (root / "src" / "other.py").read_bytes()
    with pytest.raises(err.FsError):
        svc.multi_file_patch([
            {"path": "src/app.py", "operation": "replace", "oldText": "alpha", "newText": "ALPHA"},
            {"path": "src/other.py", "operation": "replace", "oldText": "MISSING", "newText": "X"},
        ], atomic=True)
    assert (root / "src" / "app.py").read_bytes() == app_before
    assert (root / "src" / "other.py").read_bytes() == other_before


def test_multi_file_patch_success(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    result = svc.multi_file_patch([
        {"path": "src/app.py", "operation": "replace", "oldText": "alpha", "newText": "ALPHA"},
        {"path": "src/other.py", "operation": "replace", "oldText": "UNTOUCHED", "newText": "TOUCHED"},
    ])
    assert result["filesChanged"] == 2
    assert (root / "src" / "app.py").read_text(encoding="utf-8").startswith("ALPHA")


def test_multi_file_patch_rolls_back_an_apply_phase_failure(tmp_path, monkeypatch):
    """Even if the validation phase is bypassed, a mid-apply failure restores
    every file the change touched (the outer transaction snapshot)."""
    root = tree(tmp_path)
    svc = make_service(root)
    app_before = (root / "src" / "app.py").read_bytes()
    other_before = (root / "src" / "other.py").read_bytes()

    import harness.code.fs.editing as editing

    real_apply_one = editing._apply_one
    calls = {"n": 0}

    def _flaky(ctx, patch, *, dry_run, txn=None):
        if not dry_run:
            calls["n"] += 1
            if calls["n"] >= 2:
                from harness.code.fs import errors as _err
                raise _err.FsError(_err.PATCH_FAILED, "simulated apply failure")
        return real_apply_one(ctx, patch, dry_run=dry_run, txn=txn)

    monkeypatch.setattr(editing, "_apply_one", _flaky)
    with pytest.raises(err.FsError) as exc:
        svc.multi_file_patch([
            {"path": "src/app.py", "operation": "replace", "oldText": "alpha", "newText": "ALPHA"},
            {"path": "src/other.py", "operation": "replace", "oldText": "UNTOUCHED", "newText": "TOUCHED"},
        ], atomic=True)
    assert exc.value.code == err.PATCH_FAILED
    assert (root / "src" / "app.py").read_bytes() == app_before
    assert (root / "src" / "other.py").read_bytes() == other_before


def test_tolerates_typographic_quotes_and_dashes(tmp_path):
    """A curly quote or em dash in the file must not refuse an intended edit."""
    root = tree(tmp_path)
    (root / "src" / "style.ts").write_text(
        "const label = \u201cdone\u201d;\nconst range = 1\u20135;\n", encoding="utf-8"
    )
    svc = make_service(root)
    svc.replace_text("src/style.ts", 'const label = "done";', 'const label = "shipped";')
    assert (root / "src" / "style.ts").read_text(encoding="utf-8").startswith(
        'const label = "shipped";'
    )


def test_tolerates_trailing_whitespace_per_line(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "ws.py").write_text("def f():\n    return 1   \n", encoding="utf-8")
    svc = make_service(root)
    svc.replace_text("src/ws.py", "def f():\n    return 1", "def f():\n    return 2")
    assert (root / "src" / "ws.py").read_text(encoding="utf-8") == "def f():\n    return 2   \n"


def test_a_miss_returns_the_closest_lines(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "app.py").write_text(
        "alpha\nbeta\ngamma\n", encoding="utf-8"
    )
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.replace_text("src/app.py", "beta\ndelta", "x")
    assert exc.value.code == err.PATCH_CONTEXT_MISMATCH
    hint = (exc.value.details or {}).get("hint") or ""
    assert "2: beta" in hint
    assert "1: alpha" not in hint  # only the span around the match is revealed


def test_a_crlf_file_keeps_crlf_when_the_replacement_uses_lf(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "win.ts").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    svc = make_service(root)
    svc.replace_text("src/win.ts", "two", "TWO\nEXTRA")
    assert (root / "src" / "win.ts").read_bytes() == b"one\r\nTWO\r\nEXTRA\r\nthree\r\n"


def test_replace_all_replaces_every_occurrence(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "dup.txt").write_text("same\nsame\n", encoding="utf-8")
    svc = make_service(root)
    result = svc.replace_text("src/dup.txt", "same", "done", count=0)
    assert result["replaced"] == 2
    assert (root / "src" / "dup.txt").read_text(encoding="utf-8") == "done\ndone\n"


def test_regex_replacement_leaves_untouched_endings_alone(tmp_path):
    root = tree(tmp_path)
    # CRLF-dominant with one bare LF: the edit must not normalize the other line.
    (root / "src" / "mix.txt").write_bytes(b"one\r\ntwo\nthree\r\n")
    svc = make_service(root)
    svc.replace_text("src/mix.txt", "two", "TWO\nEXTRA", regex=True)
    assert (root / "src" / "mix.txt").read_bytes() == b"one\r\nTWO\r\nEXTRA\nthree\r\n"


def test_dry_run_writes_nothing(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    before = (root / "src" / "app.py").read_bytes()
    result = svc.patch_file("src/app.py", "replace", old_text="beta", new_text="BETA", dry_run=True)
    assert result["dryRun"] is True
    assert "diff" in result
    assert (root / "src" / "app.py").read_bytes() == before


def test_patch_preserves_crlf_and_bom(tmp_path):
    root = tree(tmp_path)
    target = root / "src" / "win.txt"
    target.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    svc = make_service(root)
    svc.replace_text("src/win.txt", "b", "B", expected_occurrences=1)
    data = target.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert data == "\ufeffa\r\nB\r\n".encode("utf-8")


def test_patch_reports_before_and_after_hashes(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    result = svc.patch_file("src/app.py", "replace", old_text="beta", new_text="BETA")
    assert result["sha256Before"] != result["sha256After"]


def test_append_and_prepend(tmp_path):
    root = tree(tmp_path)
    svc = make_service(root)
    svc.patch_file("src/other.py", "append", new_text="ADDED = 1\n")
    assert (root / "src" / "other.py").read_text(encoding="utf-8").endswith("ADDED = 1\n")
    svc.patch_file("src/other.py", "prepend", new_text="FIRST = 0\n")
    assert (root / "src" / "other.py").read_text(encoding="utf-8").startswith("FIRST = 0\n")


def test_binary_edit_refused(tmp_path):
    root = tree(tmp_path)
    (root / "src" / "blob.bin").write_bytes(b"\x00\x01\x02PNG")
    svc = make_service(root)
    with pytest.raises(err.FsError) as exc:
        svc.patch_file("src/blob.bin", "replace", old_text="x", new_text="y")
    assert exc.value.code == err.UNSUPPORTED_BINARY_OPERATION
