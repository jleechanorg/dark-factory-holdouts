"""Sealed holdout checks for the level-up session state-machine refactor.

Each check drives the candidate repo's ``mvp_site.level_up_session`` reducer
through a real BDD scenario and returns the string ``"PASS"`` on success.
Any assertion failure or unexpected exception surfaces as a non-PASS result,
which the evaluator buckets without leaking detail to the implementing agent.

The reducer module must stay pure (stdlib-only imports); if a refactor adds
heavy dependencies, these imports fail and the holdout fails closed — that is
intentional enforcement of the spec's purity requirement.
"""

from __future__ import annotations

import copy
import importlib
import os
import sys


def _reducer():
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    return importlib.import_module("mvp_site.level_up_session")


def check_finish_commit_clears_session() -> str:
    """Scenario 1: lvl-finish-commit-clears-session.

    Given a committing session 14 -> 15 and a model response that persists
    level 15 with no level_up_signal, complete_finish_commit must land
    status=complete with completed_at_story_id recorded, level 15, zero
    invariant violations, and a second commit must raise (double-commit
    guard).
    """
    lus = _reducer()
    gs: dict = {
        "player_character_data": {"level": 14, "name": "Holdout PC"},
        "custom_campaign_state": {},
    }
    gs = lus.apply_model_level_up_signal(
        gs, {"current_level": 14, "target_level": 15}, source_story_id="story-0"
    )
    gs = lus.enter_level_up_session(
        gs, source_choice_id="level_up_now", story_id="story-0"
    )
    gs = lus.begin_finish_commit(
        gs, source_choice_id="finish_level_up", story_id="story-1"
    )
    out = lus.complete_finish_commit(
        gs,
        player_character_data={"level": 15, "name": "Holdout PC"},
        story_id="story-1",
    )
    sess = out["level_up_session"]
    assert sess["status"] == "complete", f"status={sess['status']!r}"
    assert sess["completed_at_story_id"] == "story-1", sess
    assert out["player_character_data"]["level"] == 15, out["player_character_data"]
    violations = lus.assert_level_up_invariants(out)
    assert violations == [], violations
    try:
        lus.complete_finish_commit(
            out,
            player_character_data={"level": 15, "name": "Holdout PC"},
            story_id="story-2",
        )
    except ValueError:
        return "PASS"
    return "FAIL: second complete_finish_commit did not raise"


def check_rejected_godmode_commit_session_unchanged() -> str:
    """Scenario 2: lvl-rejected-mixed-contract (reducer-level grounding).

    Given an available session 17 -> 18, a god-mode admin commit whose
    persisted level violates the contract (below target) must raise
    ValueError, leave the input game_state byte-identical, emit no phantom
    legacy level_up_* flags, and keep invariants clean.
    """
    lus = _reducer()
    gs: dict = {
        "player_character_data": {"level": 17, "name": "Holdout PC"},
        "custom_campaign_state": {},
    }
    gs = lus.apply_model_level_up_signal(
        gs, {"current_level": 17, "target_level": 18}, source_story_id="s-1"
    )
    before = copy.deepcopy(gs)
    try:
        lus.apply_god_mode_admin_commit(
            gs,
            player_character_data={"level": 17, "name": "Holdout PC"},
            source_story_id="s-2",
        )
        return "FAIL: level guard did not raise on below-target god-mode commit"
    except ValueError:
        pass
    assert gs == before, "rejected commit mutated the input game_state"
    ccs = gs.get("custom_campaign_state") or {}
    for key in ("level_up_pending", "level_up_in_progress", "level_up_complete"):
        assert key not in ccs, f"phantom legacy flag {key} present after rejection"
    violations = lus.assert_level_up_invariants(gs)
    assert violations == [], violations
    return "PASS"


def check_stale_signal_cleared_after_admin() -> str:
    """Scenario 3: lvl-stale-signal-cleared-after-admin.

    Given a level-up available 17 -> 18, a direct god-mode commit of level
    18 (no modal choices) must complete the session, and the legacy
    projector must emit NO level_up_signal and no truthy level_up_pending —
    nothing left to override the god-mode level on the next turn.
    """
    lus = _reducer()
    gs: dict = {
        "player_character_data": {"level": 17, "name": "Holdout PC"},
        "custom_campaign_state": {},
    }
    gs = lus.apply_model_level_up_signal(
        gs, {"current_level": 17, "target_level": 18}, source_story_id="s-1"
    )
    out = lus.apply_god_mode_admin_commit(
        gs,
        player_character_data={"level": 18, "name": "Holdout PC"},
        source_story_id="s-2",
    )
    sess = out["level_up_session"]
    assert sess["status"] == "complete", f"status={sess['status']!r}"
    patch = lus.project_legacy_level_up_fields(out)
    assert "level_up_signal" not in patch, patch
    ccs_patch = patch.get("custom_campaign_state") or {}
    assert ccs_patch.get("level_up_pending") is not True, patch
    return "PASS"
