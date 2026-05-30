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
_CALL_RUNNER_SOURCE = _CALL_RUNNER.read_text()
_SUBPROCESS_TIMEOUT_SECONDS = 30

# Buckets surfaced as `detail`. Never include real values, arities, or paths.
_BUCKET_OK = "ok"
_BUCKET_VALUE_MISMATCH = "value_mismatch"
_BUCKET_ARITY_MISMATCH = "arity_mismatch"
_BUCKET_EXCEPTION = "exception"
_BUCKET_TIMEOUT = "timeout"
_BUCKET_MISSING_MODULE = "missing_module"
_BUCKET_MISSING_FUNCTION = "missing_function"
_BUCKET_MISSING_ATTRIBUTE = "missing_attribute"
_BUCKET_UNKNOWN_KIND = "unknown_kind"
_BUCKET_INVALID_SCHEMA = "invalid_schema"
_BUCKET_COMMAND_FAILED = "command_failed"
_BUCKET_OUTPUT_MISMATCH = "output_mismatch"


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


_CURRENT_FEATURE_DIR = None

def _sandbox_command(impl_root: pathlib.Path) -> list[str] | None:
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        return None
    sealed_root = str(pathlib.Path(__file__).parent.parent.resolve())
    sealed_root = sealed_root.replace("\\", "\\\\").replace('"', '\\"')
    writable_root = str(impl_root.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    
    allow_rule = ""
    global _CURRENT_FEATURE_DIR
    if _CURRENT_FEATURE_DIR:
        allowed = str(_CURRENT_FEATURE_DIR.resolve()).replace("\\", "\\\\").replace('"', '\\"')
        allow_rule = f'(allow file-read* (subpath "{allowed}"))'

    profile = f"""
(version 1)
(allow default)
(deny file-read* (subpath "{sealed_root}"))
{allow_rule}
(deny file-write* (subpath "{sealed_root}"))
(deny file-write* (require-not (subpath "{writable_root}")))
"""
    return [sandbox_exec, "-p", profile]


def _subprocess_python() -> str:
    sealed_root = pathlib.Path(__file__).parent.parent.resolve()
    candidates = [
        pathlib.Path(getattr(sys, "_base_executable", "")),
        pathlib.Path(shutil.which("python3") or ""),
        pathlib.Path(sys.executable),
    ]
    for candidate in candidates:
        if not str(candidate):
            continue
        try:
            candidate.resolve().relative_to(sealed_root)
        except ValueError:
            return str(candidate)
        except OSError:
            continue
    return sys.executable


def _run_subprocess(spec_payload: dict, impl_root: pathlib.Path) -> dict:
    """Invoke the call runner. Returns parsed JSON or a bucket-error dict."""
    sandbox_prefix = _sandbox_command(impl_root)
    if sandbox_prefix is None:
        return {"ok": False, "error": _BUCKET_EXCEPTION}
    try:
        tmp_dir = impl_root / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        env = _sanitized_env()
        env["TMPDIR"] = str(tmp_dir.resolve())
        proc = subprocess.run(
            sandbox_prefix + [_subprocess_python(), "-c", _CALL_RUNNER_SOURCE],
            input=json.dumps(spec_payload),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            cwd=str(impl_root),
            env=env,
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


def _run_shell_subprocess(command: str, impl_root: pathlib.Path) -> dict:
    """Run a shell command inside the sealed sandbox, cwd=impl_root.

    Reuses the same sandbox profile as the Python call runner so the
    implementation under test still cannot read the sealed holdouts repo.
    Returns {"ok": True, "returncode": int, "stdout": str} or a bucket error.
    """
    sandbox_prefix = _sandbox_command(impl_root)
    if sandbox_prefix is None:
        return {"ok": False, "error": _BUCKET_EXCEPTION}
    try:
        env = _sanitized_env()
        proc = subprocess.run(
            sandbox_prefix + ["/bin/sh", "-c", command],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            cwd=str(impl_root),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": _BUCKET_TIMEOUT}
    except Exception:
        return {"ok": False, "error": _BUCKET_EXCEPTION}
    finally:
        _remove_holdout_handles(impl_root)
    return {"ok": True, "returncode": proc.returncode, "stdout": proc.stdout or ""}


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


def _python_module_attr(spec: dict, impl_root: pathlib.Path) -> tuple[bool, str]:
    payload = {
        "kind": "python_module_attr",
        "module": spec["module"],
        "attribute": spec["attribute"],
    }
    result = _run_subprocess(payload, impl_root)
    if not result.get("ok"):
        return False, result.get("error") or _BUCKET_EXCEPTION
    expected_repr = repr(spec["expect_value"])
    if result.get("value") == expected_repr:
        return True, _BUCKET_OK
    return False, _BUCKET_VALUE_MISMATCH


def _shell(spec: dict, impl_root: pathlib.Path) -> tuple[bool, str]:
    """Run a shell command in the impl root; pass on exit 0 (or expect_exit).

    Spec keys:
        command (str, required): shell command to run, cwd = impl_root.
        expect_exit (int, optional, default 0): required exit code.
        expect_stdout_contains (str, optional): substring required in stdout.

    Only a redacted bucket label leaks to the implementing agent — never the
    command text, expected exit code, or actual output.
    """
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        return False, _BUCKET_INVALID_SCHEMA
    expect_exit = spec.get("expect_exit", 0)
    if not isinstance(expect_exit, int):
        return False, _BUCKET_INVALID_SCHEMA
    result = _run_shell_subprocess(command, impl_root)
    if not result.get("ok"):
        return False, result.get("error") or _BUCKET_EXCEPTION
    if result.get("returncode") != expect_exit:
        return False, _BUCKET_COMMAND_FAILED
    expect_contains = spec.get("expect_stdout_contains")
    if expect_contains is not None:
        if not isinstance(expect_contains, str):
            return False, _BUCKET_INVALID_SCHEMA
        if expect_contains not in result.get("stdout", ""):
            return False, _BUCKET_OUTPUT_MISMATCH
    return True, _BUCKET_OK


EVALUATORS = {
    "python_call": _python_call,
    "python_call_signature": _python_call_signature,
    "python_module_attr": _python_module_attr,
    "shell": _shell,
}


def evaluate(feature: str, impl_root: pathlib.Path) -> dict:
    global _CURRENT_FEATURE_DIR
    _CURRENT_FEATURE_DIR = pathlib.Path(__file__).parent.parent / "holdouts" / feature
    
    # Add to PYTHONPATH
    existing_pp = os.environ.get("PYTHONPATH", "")
    if existing_pp:
        os.environ["PYTHONPATH"] = f"{_CURRENT_FEATURE_DIR}:{existing_pp}"
    else:
        os.environ["PYTHONPATH"] = str(_CURRENT_FEATURE_DIR)

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
