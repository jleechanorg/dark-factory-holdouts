"""Regression tests for the sealed-holdout leak channels.

These verify that the evaluator:
  1. Never emits the expected scenario value on a failing run.
  2. Never emits the impl's actual return value.
  3. Prevents the impl from reading/persisting scenarios.yaml.
  4. Redacts arity mismatches.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EVALUATOR = REPO_ROOT / "evaluator" / "run.py"
EXPECTED_STRING = "Hello, world!"
SCENARIOS_PATH = REPO_ROOT / "holdouts" / "hello" / "scenarios.yaml"


def _write_impl(tmp_path: pathlib.Path, greet_source: str) -> pathlib.Path:
    impl_root = tmp_path / "impl_root"
    impl_pkg = impl_root / "impl"
    impl_pkg.mkdir(parents=True)
    (impl_pkg / "__init__.py").write_text("")
    (impl_pkg / "greet.py").write_text(greet_source)
    return impl_root


def _run_evaluator(impl_root: pathlib.Path) -> tuple[str, dict]:
    proc = subprocess.run(
        [sys.executable, str(EVALUATOR), "--feature", "hello", "--implementation", str(impl_root)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    # The final JSON line is the last non-empty stdout line.
    json_line = ""
    for line in reversed((proc.stdout or "").splitlines()):
        stripped = line.strip()
        if stripped.startswith("{"):
            json_line = stripped
            break
    payload = json.loads(json_line) if json_line else {}
    return combined, payload


def _write_scenarios(feature: str, body: str) -> pathlib.Path:
    feature_dir = REPO_ROOT / "holdouts" / feature
    feature_dir.mkdir(parents=True, exist_ok=True)
    path = feature_dir / "scenarios.yaml"
    path.write_text(body)
    return path


def _find_scenario(payload: dict, name: str) -> dict:
    for sc in payload.get("scenarios", []):
        if sc.get("name") == name:
            return sc
    raise AssertionError(f"scenario {name!r} not in payload: {payload}")


def test_evaluator_does_not_leak_expected_values(tmp_path: pathlib.Path) -> None:
    """A wrong-return impl must not leak the expected string or the got string."""
    wrong_return = "WRONG_RETURN_VALUE_SENTINEL_8417"
    impl_root = _write_impl(
        tmp_path,
        f"def hello():\n    return {wrong_return!r}\n",
    )
    output, payload = _run_evaluator(impl_root)

    assert EXPECTED_STRING not in output, (
        "expected scenario value leaked to evaluator output"
    )
    assert wrong_return not in output, (
        "impl's actual return value leaked to evaluator output"
    )

    sc = _find_scenario(payload, "hello_returns_world_string")
    assert sc["status"] == "fail"
    assert sc["detail"] == "value_mismatch"


def test_malicious_impl_cannot_passthrough_scenario_yaml(tmp_path: pathlib.Path) -> None:
    """An impl that reads scenarios.yaml can't echo it through the leak channel."""
    scenarios_path_literal = str(SCENARIOS_PATH)
    malicious_source = (
        "def hello():\n"
        "    try:\n"
        f"        with open({scenarios_path_literal!r}) as fh:\n"
        "            return fh.read()\n"
        "    except Exception as e:\n"
        "        return f'read_failed: {e!r}'\n"
    )
    impl_root = _write_impl(tmp_path, malicious_source)
    output, payload = _run_evaluator(impl_root)

    sc = _find_scenario(payload, "hello_returns_world_string")
    assert sc["status"] == "fail"
    assert sc["detail"] == "value_mismatch"

    # Even if the impl successfully read the YAML, none of its contents may
    # appear in the evaluator's emitted output.
    assert "expect_return:" not in output
    assert EXPECTED_STRING not in output
    assert "expect_arity:" not in output


def test_malicious_impl_cannot_persist_scenario_yaml(tmp_path: pathlib.Path) -> None:
    """The implementation subprocess must not be able to read sealed YAML."""
    leak_path = tmp_path / "leaked_holdout.txt"
    scenarios_path_literal = str(SCENARIOS_PATH)
    leak_path_literal = str(leak_path)
    malicious_source = (
        "def hello():\n"
        f"    with open({scenarios_path_literal!r}) as fh:\n"
        "        data = fh.read()\n"
        f"    with open({leak_path_literal!r}, 'w') as out:\n"
        "        out.write(data)\n"
        "    return 'Hello, world!'\n"
    )
    impl_root = _write_impl(tmp_path, malicious_source)
    output, payload = _run_evaluator(impl_root)

    sc = _find_scenario(payload, "hello_returns_world_string")
    assert sc["status"] == "fail"
    assert sc["detail"] == "exception"
    assert not leak_path.exists(), output


def test_malicious_impl_cannot_cat_scenario_yaml(tmp_path: pathlib.Path) -> None:
    leak_path = tmp_path / "cat_leak.txt"
    malicious_source = (
        "import subprocess\n"
        "def hello():\n"
        f"    data = subprocess.check_output(['cat', {str(SCENARIOS_PATH)!r}], text=True)\n"
        f"    with open({str(leak_path)!r}, 'w') as out:\n"
        "        out.write(data)\n"
        "    return 'Hello, world!'\n"
    )
    impl_root = _write_impl(tmp_path, malicious_source)
    output, payload = _run_evaluator(impl_root)

    sc = _find_scenario(payload, "hello_returns_world_string")
    assert sc["status"] == "fail"
    assert sc["detail"] == "exception"
    assert not leak_path.exists(), output


def test_malicious_impl_cannot_persist_scenario_symlink_handle(tmp_path: pathlib.Path) -> None:
    link_path = tmp_path / "persisted_scenario_link.yaml"
    malicious_source = (
        "import os\n"
        "def hello():\n"
        f"    os.symlink({str(SCENARIOS_PATH)!r}, {str(link_path)!r})\n"
        "    return 'Hello, world!'\n"
    )
    impl_root = _write_impl(tmp_path, malicious_source)
    output, _payload = _run_evaluator(impl_root)

    assert not link_path.exists(), output


def test_malicious_impl_cannot_read_scenario_via_symlink(tmp_path: pathlib.Path) -> None:
    leak_path = tmp_path / "symlink_leak.txt"
    link_path = tmp_path / "scenario_link.yaml"
    malicious_source = (
        "import os\n"
        "def hello():\n"
        f"    os.symlink({str(SCENARIOS_PATH)!r}, {str(link_path)!r})\n"
        f"    with open({str(link_path)!r}) as fh:\n"
        "        data = fh.read()\n"
        f"    with open({str(leak_path)!r}, 'w') as out:\n"
        "        out.write(data)\n"
        "    return 'Hello, world!'\n"
    )
    impl_root = _write_impl(tmp_path, malicious_source)
    output, payload = _run_evaluator(impl_root)

    sc = _find_scenario(payload, "hello_returns_world_string")
    assert sc["status"] == "fail"
    assert sc["detail"] == "exception"
    assert not leak_path.exists(), output
    assert not link_path.exists(), output


def test_malicious_impl_cannot_glob_and_read_holdouts(tmp_path: pathlib.Path) -> None:
    leak_path = tmp_path / "glob_leak.txt"
    holdouts_glob = str(REPO_ROOT / "holdouts" / "**" / "*.yaml")
    malicious_source = (
        "import glob\n"
        "def hello():\n"
        f"    matches = glob.glob({holdouts_glob!r}, recursive=True)\n"
        "    data = ''.join(open(path).read() for path in matches)\n"
        f"    with open({str(leak_path)!r}, 'w') as out:\n"
        "        out.write(data)\n"
        "    return 'Hello, world!'\n"
    )
    impl_root = _write_impl(tmp_path, malicious_source)
    output, payload = _run_evaluator(impl_root)

    sc = _find_scenario(payload, "hello_returns_world_string")
    assert sc["status"] == "fail"
    assert sc["detail"] in {"exception", "value_mismatch"}
    assert not leak_path.exists(), output


def test_empty_scenario_file_fails_closed() -> None:
    feature = "empty_schema_regression"
    path = _write_scenarios(feature, "feature: empty_schema_regression\nscenarios: []\n")
    try:
        proc = subprocess.run(
            [sys.executable, str(EVALUATOR), "--feature", feature, "--implementation", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert proc.returncode == 1
        assert '"verdict": "fail"' in proc.stdout
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def test_missing_scenarios_key_fails_closed() -> None:
    feature = "missing_schema_key_regression"
    path = _write_scenarios(feature, "feature: missing_schema_key_regression\nscenarioz: []\n")
    try:
        proc = subprocess.run(
            [sys.executable, str(EVALUATOR), "--feature", feature, "--implementation", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        assert proc.returncode == 1
        assert '"verdict": "fail"' in proc.stdout
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def test_invalid_yaml_fails_closed_without_traceback() -> None:
    feature = "invalid_yaml_regression"
    path = _write_scenarios(feature, "feature: [unterminated\n")
    try:
        proc = subprocess.run(
            [sys.executable, str(EVALUATOR), "--feature", feature, "--implementation", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 1
        assert '"verdict": "fail"' in proc.stdout
        assert "Traceback" not in combined
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def test_null_scenario_entry_fails_closed_without_traceback() -> None:
    feature = "null_scenario_regression"
    path = _write_scenarios(feature, "feature: null_scenario_regression\nscenarios:\n  - null\n")
    try:
        proc = subprocess.run(
            [sys.executable, str(EVALUATOR), "--feature", feature, "--implementation", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 1
        assert '"verdict": "fail"' in proc.stdout
        assert "Traceback" not in combined
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def test_scenario_missing_eval_fails_closed_without_traceback() -> None:
    feature = "missing_eval_regression"
    path = _write_scenarios(
        feature,
        "feature: missing_eval_regression\nscenarios:\n  - name: missing_eval\n",
    )
    try:
        proc = subprocess.run(
            [sys.executable, str(EVALUATOR), "--feature", feature, "--implementation", str(REPO_ROOT)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
        combined = proc.stdout + proc.stderr
        assert proc.returncode == 1
        assert '"verdict": "fail"' in proc.stdout
        assert "Traceback" not in combined
    finally:
        path.unlink(missing_ok=True)
        path.parent.rmdir()


def test_arity_mismatch_redacted(tmp_path: pathlib.Path) -> None:
    """A wrong-arity impl must surface 'arity_mismatch' with no integers."""
    impl_root = _write_impl(
        tmp_path,
        "def hello(x):\n    return 'Hello, world!'\n",
    )
    output, payload = _run_evaluator(impl_root)

    sc = _find_scenario(payload, "hello_takes_no_arguments")
    assert sc["status"] == "fail"
    assert sc["detail"] == "arity_mismatch"

    assert "expect_arity:" not in output
    # The actual arity (1) and the expected arity (0) must not appear as
    # standalone "arity N" reports anywhere in the output stream.
    assert "arity 1" not in output
    assert "arity 0" not in output
