# Dark Factory — Sealed Holdouts

This repo holds **blind evaluation scenarios** for the Dark Factory.

## Access Boundary

The coding agent operates in `~/projects/dark-factory/` and **MUST NEVER** read
or list this directory. The evaluator runs here in a separate process with its
own working directory.

If an agent path touches `dark-factory-holdouts/` during a pipeline run, the
adversarial guarantee is broken and the run is invalid.

## Layout

```
holdouts/
└── <feature>/
    ├── scenarios.yaml      # BDD scenarios (Given/When/Then)
    └── eval.py             # Optional evaluator (defaults to scenarios.yaml runner)
```

## Scenario Format

```yaml
feature: rate_limiter
scenarios:
  - name: 6th request in window is rejected
    given:
      - 5 campaigns created by user_abc in last 60 minutes
    when: POST /api/campaign by user_abc
    then:
      status: 429
      body_contains: rate_limit_exceeded
      body_has_field: retry_after_sec
```

## Verdict

The evaluator emits one of `PASS | WARN | FAIL` per scenario and an overall
verdict. Only the verdict and scenario names (NOT the scenario body) flow back
to the implementing agent on failure.
