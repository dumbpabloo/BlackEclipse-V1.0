"""`eclipse kernel` CLI: dump, diff and doctor without mounting."""

from __future__ import annotations

import json

from harness.kernel.cli import run_cli


def _profile(tmp_path):
    profile = tmp_path / "profiles" / "demo"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(
        json.dumps({"name": "demo", "bundles": ["base"], "patch": "cordis.patch.yaml"}),
        encoding="utf-8",
    )
    (profile / "cordis.patch.yaml").write_text(
        "- insert:\n    - id: tools\n      name: pkg.tools:apply\n      config: {limit: 10}\n",
        encoding="utf-8",
    )
    bundles = tmp_path / "bundles"
    base = bundles / "base"
    base.mkdir(parents=True)
    (base / "plugin.yaml").write_text("id: base\nbundle:\n  patch: cordis.patch.yaml\n", encoding="utf-8")
    (base / "cordis.patch.yaml").write_text(
        "- insert:\n    - id: base\n      name: pkg.base:apply\n", encoding="utf-8",
    )
    return profile, bundles


def test_dump_includes_profile_and_bundle_rows(tmp_path, capsys):
    profile, bundles = _profile(tmp_path)
    assert run_cli(["dump", "--profile-dir", str(profile), "--bundle-path", str(bundles)]) == 0
    output = capsys.readouterr().out
    assert "id: base" in output
    assert "id: tools" in output
    assert "limit: 10" in output


def test_doctor_reports_no_problems(tmp_path, capsys):
    profile, bundles = _profile(tmp_path)
    assert run_cli(["doctor", "--profile-dir", str(profile), "--bundle-path", str(bundles)]) == 0
    assert "no problems" in capsys.readouterr().out


def test_diff_reports_the_overlay_change(tmp_path, capsys):
    profile, bundles = _profile(tmp_path)
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("- id: tools\n  config: {limit: 20}\n", encoding="utf-8")
    assert run_cli([
        "diff", "--profile-dir", str(profile), "--bundle-path", str(bundles), "--patch", str(overlay),
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in payload["changed"]] == ["tools"]


def test_main_dispatches_the_kernel_group(tmp_path, capsys):
    from harness import cli

    profile, bundles = _profile(tmp_path)
    assert cli.main([
        "kernel", "doctor", "--profile-dir", str(profile), "--bundle-path", str(bundles),
    ]) == 0
    assert "no problems" in capsys.readouterr().out
