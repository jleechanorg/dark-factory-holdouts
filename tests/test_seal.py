"""Regression tests for the sealed-holdout leak channels.

These verify that the evaluator:
  1. Never emits the expected scenario value on a failing run.
  2. Never emits the impl's actual return value.
  3. Closes the leak channel even when the impl tries to read scenarios.yaml.
  4. Redacts arity mismatches.

Future work: pair these tests with sandbox-exec (or a sibling seccomp profile)
to enforce true filesystem isolation. Today the contract is only that the
*output* channel is sealed; an impl can still read disk inside its subprocess.
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
    """An impl that reads scenarios.yaml can't echo it through the leak channel.

    This proves only that the *output channel* is closed — not that the file is
    unreadable. Future work could add sandbox-exec for true FS isolation.
    """
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
