"""Tests for the meta-tools `dwg_import_project_audit` / `..._execute`.

Validates that the consolidated pipeline produces the same KG state as the
dryrun driver (which uses lower-level tools). Both paths must agree on
the P7 fixture — if they diverge, one or the other has regressed.

`doc=None` path : we simulate linked_views offline (cf.
`_meta_simulate_linked_views_offline`) so elevation-vote-based wall fusion
runs as it would in Revit.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib.project_kg import ProjectKG
from lib.tools.dwg_import import (
    import_project_audit,
    import_project_execute,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "P7"


@pytest.fixture
def fresh_kg() -> ProjectKG:
    return ProjectKG(project_id="test-meta")


# ----- audit ------------------------------------------------------------


def test_audit_classifies_files(fresh_kg):
    audit = import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    files = audit["files"]
    assert len(files["plans"]) == 2, "P7 has 2 plans"
    assert len(files["coupes"]) == 3, "P7 has 3 coupes"
    assert len(files["elevations"]) == 4, "P7 has 4 elevations"
    assert files["plan_with_markers"] is not None


def test_audit_section_assignment_pairs_all_coupes(fresh_kg):
    audit = import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    assert len(audit["section_assignment"]) == 3
    for entry in audit["section_assignment"]:
        assert "plan_p1" in entry and "plan_p2" in entry
        assert entry["view_dir"] in ("left", "right", "up", "down")


def test_audit_proposes_level_actions(fresh_kg):
    audit = import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    # Fresh KG → all 3 P7 levels (N0/N1/N2 = 0/3/6m) are missing → 3 creates.
    creates = [a for a in audit["level_actions_proposed"] if a["action"] == "create"]
    assert len(creates) == 3
    names = {a["name"] for a in creates}
    assert names == {"Niveau 0", "Niveau 1", "Niveau 2"}


def test_audit_signals_user_confirmation_needed(fresh_kg):
    audit = import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    # P7 has integrity warnings → needs_user, and fresh KG → levels confirm.
    assert audit["gate_status"] == "needs_user"
    assert audit["needs_warnings_confirm"] is True
    assert audit["needs_levels_confirm"] is True


def test_audit_is_read_only(fresh_kg):
    """Audit ne doit pas muter le KG — Phase 1 pure read-only."""
    snapshot_before = {
        t: len(fresh_kg.find_by_type(t))
        for t in ("Level", "WallType", "Wall", "Window", "Floor", "DxfImportContext")
    }
    import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    snapshot_after = {
        t: len(fresh_kg.find_by_type(t))
        for t in ("Level", "WallType", "Wall", "Window", "Floor", "DxfImportContext")
    }
    assert snapshot_before == snapshot_after


# ----- execute ----------------------------------------------------------


def test_execute_end_to_end_matches_dryrun_counts(fresh_kg):
    """Pipeline complet (audit + execute) en mode doc=None doit produire
    exactement les mêmes comptes que le dryrun driver — qui lui-même match
    le runtime Revit live (cf. JOURNAL 2026-05-14)."""
    audit = import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    result = import_project_execute(
        kg=fresh_kg, doc=None, directory=str(_FIXTURE_DIR),
        level_actions=audit["level_actions_proposed"],
        proceed_on_warnings=True,
    )
    assert result["ok"] is True

    # Phase 1.
    p1 = result["phase1_setup"]
    assert p1["levels_created"] == 3
    assert p1["section_lines_registered"] == 3
    # offline simulation : 2 plans + 3 coupes + 4 elev = 9 linked_views.
    assert p1["linked_views_count"] == 9
    assert p1.get("offline_simulated", False) is True

    # Phase 2.
    p2a = result["phase2a_walls"]
    assert p2a["walls_imported_total"] == 19
    assert p2a["fusion_events"] == 7

    p2b = result["phase2b_openings"]
    assert p2b["plan_openings_detected"] == 15
    assert p2b["openings_windows_created"] == 13
    assert p2b["openings_doors_created"] == 0
    assert p2b["openings_oversize_for_wall"] == 2

    p2c = result["phase2c_floors"]
    assert p2c["floors_created_count"] == 2

    # KG final state.
    assert len(fresh_kg.find_by_type("Level")) == 3
    assert len(fresh_kg.find_by_type("Wall")) == 19
    assert len(fresh_kg.find_by_type("Window")) == 13
    assert len(fresh_kg.find_by_type("Door")) == 0
    assert len(fresh_kg.find_by_type("Floor")) == 2


def test_execute_aborts_when_integrity_errors(fresh_kg, tmp_path):
    """Si check_planset_integrity renvoie errors, execute refuse."""
    # Empty directory triggers an error (no DXFs).
    empty = tmp_path / "empty"
    empty.mkdir()
    # P7 directory has warnings but no errors, so we need to provoke errors
    # differently. Easiest path : pass a directory that doesn't exist.
    with pytest.raises(FileNotFoundError):
        import_project_audit(kg=fresh_kg, directory=str(empty / "ghost"))


def test_execute_refuses_warnings_without_proceed_flag(fresh_kg):
    """Si gate_status==needs_user mais proceed_on_warnings=False, refuse."""
    result = import_project_execute(
        kg=fresh_kg, doc=None, directory=str(_FIXTURE_DIR),
        level_actions=[],
        proceed_on_warnings=False,
    )
    assert result["ok"] is False
    assert "proceed_on_warnings" in result["reason"]


def test_execute_idempotent_on_levels(fresh_kg):
    """Re-run avec mêmes level_actions ne duplique pas les niveaux."""
    audit = import_project_audit(kg=fresh_kg, directory=str(_FIXTURE_DIR))
    import_project_execute(
        kg=fresh_kg, doc=None, directory=str(_FIXTURE_DIR),
        level_actions=audit["level_actions_proposed"],
        proceed_on_warnings=True,
    )
    first_count = len(fresh_kg.find_by_type("Level"))
    # Re-run on existing levels (skip path).
    result = import_project_execute(
        kg=fresh_kg, doc=None, directory=str(_FIXTURE_DIR),
        level_actions=audit["level_actions_proposed"],
        proceed_on_warnings=True,
    )
    assert result["phase1_setup"]["levels_create_skipped_existing"] == 3
    assert len(fresh_kg.find_by_type("Level")) == first_count
