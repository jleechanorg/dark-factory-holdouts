"""Subprocess helper: import an impl module, call a function, return repr/arity.

This helper runs in an isolated subprocess so the implementation under test
cannot read sibling files (scenarios.yaml) via the evaluator's open fds, env,
or cwd. It NEVER receives the expected value — only the evaluator process
knows that, and comparison happens in the evaluator after this helper returns.

Stdin (JSON):
    {"module": str, "function": str, "args": list, "kwargs": dict,
     "kind": "python_call" | "python_call_signature"}

Stdout (JSON, single line):
    {"ok": true,  "value": "<repr>"}            # python_call
    {"ok": true,  "arity": int}                  # python_call_signature
    {"ok": false, "error": "<bucket>"}           # any failure
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import sys

# The evaluator launches us with cwd=impl_root. Make that the import root so
# the impl package (e.g. `impl.greet`) resolves the same way it would have
# from inside the evaluator process — without ever sharing the evaluator's
# sys.path or working directory.
sys.path.insert(0, os.getcwd())


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def main() -> int:
    try:
        spec = json.loads(sys.stdin.read())
    except Exception:
        _emit({"ok": False, "error": "exception"})
        return 0

    kind = spec.get("kind")
    mod_name = spec.get("module", "")
    func_name = spec.get("function", "")

    try:
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError:
        _emit({"ok": False, "error": "missing_module"})
        return 0
    except Exception:
        _emit({"ok": False, "error": "exception"})
        return 0

    fn = getattr(mod, func_name, None)
    if fn is None:
        _emit({"ok": False, "error": "missing_function"})
        return 0

    if kind == "python_call_signature":
        try:
            arity = len(inspect.signature(fn).parameters)
        except Exception:
            _emit({"ok": False, "error": "exception"})
            return 0
        _emit({"ok": True, "arity": arity})
        return 0

    if kind == "python_call":
        args = spec.get("args", []) or []
        kwargs = spec.get("kwargs", {}) or {}
        try:
            got = fn(*args, **kwargs)
        except Exception:
            _emit({"ok": False, "error": "exception"})
            return 0
        _emit({"ok": True, "value": repr(got)})
        return 0

    _emit({"ok": False, "error": "exception"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
