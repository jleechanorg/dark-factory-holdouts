"""Sealed holdout evaluator.

Run blind against an implementation tree. Emits per-scenario verdicts and a
final aggregate JSON line consumed by the dark-factory engine.

Only the scenario name + verdict (PASS/FAIL) leaks to the implementing agent —
never the scenario body.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import pathlib
import sys
import traceback

import yaml


def _python_call(spec: dict, impl_root: pathlib.Path) -> tuple[bool, str]:
    sys.path.insert(0, str(impl_root))
    mod_name = spec["module"]
    func_name = spec["function"]
    args = spec.get("args", [])
    kwargs = spec.get("kwargs", {})
    expected = spec["expect_return"]
    try:
        # Force a fresh import so reruns reflect on-disk changes.
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, func_name)
        got = fn(*args, **kwargs)
    except Exception:
        return False, "exception: " + traceback.format_exc().splitlines()[-1]
    finally:
        sys.path.pop(0)
    if got == expected:
        return True, "ok"
    return False, f"got {got!r} expected {expected!r}"


def _python_call_signature(spec: dict, impl_root: pathlib.Path) -> tuple[bool, str]:
    sys.path.insert(0, str(impl_root))
    try:
        if spec["module"] in sys.modules:
            del sys.modules[spec["module"]]
        mod = importlib.import_module(spec["module"])
        fn = getattr(mod, spec["function"])
        sig = inspect.signature(fn)
        arity = len(sig.parameters)
        if arity == spec["expect_arity"]:
            return True, "ok"
        return False, f"arity {arity} expected {spec['expect_arity']}"
    except Exception:
        return False, "exception: " + traceback.format_exc().splitlines()[-1]
    finally:
        sys.path.pop(0)


EVALUATORS = {
    "python_call": _python_call,
    "python_call_signature": _python_call_signature,
}


def evaluate(feature: str, impl_root: pathlib.Path) -> dict:
    scenarios_path = pathlib.Path(__file__).parent.parent / "holdouts" / feature / "scenarios.yaml"
    if not scenarios_path.exists():
        return {"verdict": "fail", "error": f"no scenarios at {scenarios_path}", "scenarios": []}

    data = yaml.safe_load(scenarios_path.read_text())
    results = []
    all_pass = True
    for sc in data.get("scenarios", []):
        kind = sc["eval"]["kind"]
        evaluator = EVALUATORS.get(kind)
        if not evaluator:
            results.append({"name": sc["name"], "status": "fail", "detail": f"unknown eval kind {kind!r}"})
            all_pass = False
            continue
        ok, detail = evaluator(sc["eval"], impl_root)
        results.append({"name": sc["name"], "status": "pass" if ok else "fail", "detail": detail})
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
