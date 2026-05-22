"""Sealed holdout evaluator.

Run blind against an implementation tree. Emits per-scenario verdicts and a
final aggregate JSON line consumed by the dark-factory engine.

Only the scenario name + verdict (pass/fail) and a redacted bucket label leak
to the implementing agent — never the scenario body, expected value, or
actual return value.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys

import yaml

_CALL_RUNNER = pathlib.Path(__file__).parent / "_call_runner.py"
_SUBPROCESS_TIMEOUT_SECONDS = 30

# Buckets surfaced as `detail`. Never include real values, arities, or paths.
_BUCKET_OK = "ok"
_BUCKET_VALUE_MISMATCH = "value_mismatch"
_BUCKET_ARITY_MISMATCH = "arity_mismatch"
_BUCKET_EXCEPTION = "exception"
_BUCKET_TIMEOUT = "timeout"
_BUCKET_MISSING_MODULE = "missing_module"
_BUCKET_MISSING_FUNCTION = "missing_function"
_BUCKET_UNKNOWN_KIND = "unknown_kind"
_BUCKET_INVALID_SCHEMA = "invalid_schema"


def _invalid_schema(feature: str, name: str = "schema") -> dict:
    return {
        "verdict": "fail",
        "feature": feature,
        "scenarios": [{"name": name, "status": "fail", "detail": _BUCKET_INVALID_SCHEMA}],
    }


def _sanitized_env() -> dict:
    """Strip holdout-leaking env vars before exec'ing the impl subprocess."""
    env = {}
    for k, v in os.environ.items():
        if k == "DARK_FACTORY_HOLDOUTS":
            continue
        if "HOLDOUT" in k.upper():
            continue
        env[k] = v
    return env


def _sandbox_command(impl_root: pathlib.Path) -> list[str] | None:
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        return None
    holdouts_root = str((pathlib.Path(__file__).parent.parent / "holdouts").resolve())
    holdouts_root = holdouts_root.replace("\\", "\\\\").replace('"', '\\"')
    writable_root = str(impl_root.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    profile = f"""
(version 1)
(allow default)
(deny file-read* (subpath "{holdouts_root}"))
(deny file-write* (subpath "{holdouts_root}"))
(deny file-write* (require-not (subpath "{writable_root}")))
"""
    return [sandbox_exec, "-p", profile]


def _run_subprocess(spec_payload: dict, impl_root: pathlib.Path) -> dict:
    """Invoke the call runner. Returns parsed JSON or a bucket-error dict."""
    sandbox_prefix = _sandbox_command(impl_root)
    if sandbox_prefix is None:
        return {"ok": False, "error": _BUCKET_EXCEPTION}
    try:
        proc = subprocess.run(
            sandbox_prefix + [sys.executable, str(_CALL_RUNNER)],
            input=json.dumps(spec_payload),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            cwd=str(impl_root),
            env=_sanitized_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": _BUCKET_TIMEOUT}
    except Exception:
        return {"ok": False, "error": _BUCKET_EXCEPTION}
    finally:
        _remove_holdout_handles(impl_root)

    out = (proc.stdout or "").strip()
    if not out:
        return {"ok": False, "error": _BUCKET_EXCEPTION}
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "error": _BUCKET_EXCEPTION}


def _remove_holdout_handles(impl_root: pathlib.Path) -> None:
    holdouts_root = (pathlib.Path(__file__).parent.parent / "holdouts").resolve()
    holdout_ids = set()
    for path in holdouts_root.rglob("*"):
        try:
            if path.is_file():
                st = path.stat()
                holdout_ids.add((st.st_dev, st.st_ino))
        except OSError:
            continue

    try:
        paths = list(impl_root.rglob("*"))
    except OSError:
        return
    for path in paths:
        try:
            if path.is_symlink():
                try:
                    path.resolve(strict=True).relative_to(holdouts_root)
                except (FileNotFoundError, ValueError):
                    continue
                path.unlink()
                continue
            if path.is_file():
                st = path.stat()
                if (st.st_dev, st.st_ino) in holdout_ids:
                    path.unlink()
        except OSError:
            continue


def _python_call(spec: dict, impl_root: pathlib.Path) -> tuple[bool, str]:
    payload = {
        "kind": "python_call",
        "module": spec["module"],
        "function": spec["function"],
        "args": spec.get("args", []) or [],
        "kwargs": spec.get("kwargs", {}) or {},
    }
    result = _run_subprocess(payload, impl_root)
    if not result.get("ok"):
        return False, result.get("error") or _BUCKET_EXCEPTION
    expected_repr = repr(spec["expect_return"])
    if result.get("value") == expected_repr:
        return True, _BUCKET_OK
    return False, _BUCKET_VALUE_MISMATCH


def _python_call_signature(spec: dict, impl_root: pathlib.Path) -> tuple[bool, str]:
    payload = {
        "kind": "python_call_signature",
        "module": spec["module"],
        "function": spec["function"],
    }
    result = _run_subprocess(payload, impl_root)
    if not result.get("ok"):
        return False, result.get("error") or _BUCKET_EXCEPTION
    if result.get("arity") == spec["expect_arity"]:
        return True, _BUCKET_OK
    return False, _BUCKET_ARITY_MISMATCH


EVALUATORS = {
    "python_call": _python_call,
    "python_call_signature": _python_call_signature,
}


def evaluate(feature: str, impl_root: pathlib.Path) -> dict:
    scenarios_path = pathlib.Path(__file__).parent.parent / "holdouts" / feature / "scenarios.yaml"
    if not scenarios_path.exists():
        # Do not echo the path back to the engine — keep the leak surface narrow.
        return {"verdict": "fail", "feature": feature, "scenarios": []}

    try:
        data = yaml.safe_load(scenarios_path.read_text())
    except yaml.YAMLError:
        return _invalid_schema(feature)
    scenarios = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(scenarios, list) or not scenarios:
        return _invalid_schema(feature)
    results = []
    all_pass = True
    for sc in scenarios:
        if not isinstance(sc, dict):
            results.append({"name": "schema", "status": "fail", "detail": _BUCKET_INVALID_SCHEMA})
            all_pass = False
            continue
        name = str(sc.get("name") or "schema")
        eval_spec = sc.get("eval")
        if not isinstance(eval_spec, dict):
            results.append(
                {
                    "name": name,
                    "status": "fail",
                    "detail": _BUCKET_INVALID_SCHEMA,
                }
            )
            all_pass = False
            continue
        kind = eval_spec.get("kind")
        if not isinstance(kind, str):
            results.append(
                {
                    "name": name,
                    "status": "fail",
                    "detail": _BUCKET_INVALID_SCHEMA,
                }
            )
            all_pass = False
            continue
        evaluator = EVALUATORS.get(kind)
        if not evaluator:
            results.append({"name": name, "status": "fail", "detail": _BUCKET_UNKNOWN_KIND})
            all_pass = False
            continue
        try:
            ok, detail = evaluator(eval_spec, impl_root)
        except (KeyError, TypeError):
            ok, detail = False, _BUCKET_INVALID_SCHEMA
        results.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})
        if not ok:
            all_pass = False

    return {
        "verdict": "pass" if all_pass else "fail",
        "feature": feature,
        "scenarios": results,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dark-factory-evaluator")
    p.add_argument("--feature", required=True)
    p.add_argument("--implementation", required=True, type=pathlib.Path)
    args = p.parse_args(argv)

    result = evaluate(args.feature, args.implementation)
    # Human-readable lines first (scenario names + status only — no body leakage).
    for sc in result.get("scenarios", []):
        print(f"  {sc['status'].upper():4}  {sc['name']}")
    print()
    # Final JSON line consumed by the engine.
    print(json.dumps(result))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
