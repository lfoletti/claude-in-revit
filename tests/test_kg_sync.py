"""Tests for lib.kg_sync — helpers that don't pull on Autodesk.Revit.DB.

Two scopes covered here:
- `bind` / `llm_id_of` / `_extract_revit_id` with fake Element-like objects.
- `@kg_synced` decorator, with `lib.revit_primitives.transaction` monkeypatched
  to a stub context manager so we never need a Revit `Document` or
  `Autodesk.Revit.DB`. The lazy import inside `_wrap` resolves to the
  monkeypatched module on the second call.

`full_rescan` and `revit_id_of` (which constructs a real `ElementId`) need
Revit to be in-process — exercised at runtime in `refresh_kg.pushbutton`,
not here.
"""
from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from lib import config, kg_sync
from lib.project_kg import ProjectKG


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirect `Path.home()` so KG paths land under tmp_path."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


class FakeDoc:
    """Minimal Revit `Document` stand-in for hors-Revit tests.

    `kg_sync.project_id_for` and `open_or_create` only read `PathName` and
    `Title`; nothing else is exercised. Real Revit `Document` is unavailable
    outside pyRevit.
    """
    def __init__(self, path_name="", title="Untitled"):
        self.PathName = path_name
        self.Title = title


# ----- _extract_revit_id ------------------------------------------------


def test_extract_revit_id_from_raw_int():
    assert kg_sync._extract_revit_id(7) == 7  # noqa: SLF001


def test_extract_revit_id_from_elementid_like_via_value():
    class FakeElementId:
        Value = 12345
    assert kg_sync._extract_revit_id(FakeElementId()) == 12345  # noqa: SLF001


def test_extract_revit_id_from_element_like_via_id_value():
    class FakeElementId:
        Value = 999
    class FakeElement:
        Id = FakeElementId()
    assert kg_sync._extract_revit_id(FakeElement()) == 999  # noqa: SLF001


def test_extract_revit_id_falls_back_to_integervalue_pre_2024():
    # Pre-2024 Revit exposed IntegerValue (int), not Value (long).
    class FakeElementId:
        IntegerValue = 555
    assert kg_sync._extract_revit_id(FakeElementId()) == 555  # noqa: SLF001


# ----- bind / llm_id_of --------------------------------------------------


def _seed(kg: ProjectKG) -> tuple:
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})
    return level, wt


def test_bind_accepts_element_like_objects():
    kg = ProjectKG("p")
    level, _ = _seed(kg)

    class FakeElementId:
        Value = 4242
    class FakeElement:
        Id = FakeElementId()

    kg_sync.bind(kg, level, FakeElement())
    assert kg.get_revit_id(level) == 4242


class _FakeElementId:
    """Fake Revit `ElementId` with the post-2024 `.Value` attribute."""
    def __init__(self, value):
        self.Value = value


class _FakeCategory:
    def __init__(self, name, category_int):
        self.Name = name
        self.Id = _FakeElementId(category_int)


class _FakeElement:
    """Fake Revit `Element` with a `.Category` attribute."""
    def __init__(self, category):
        self.Category = category


class _FakeDoc:
    """Fake Revit `Document` exposing `GetElement(eid)`."""
    def __init__(self, elements_by_eid):
        self._elements = dict(elements_by_eid)  # eid_value -> _FakeElement

    def GetElement(self, eid):
        return self._elements.get(_eid_value(eid))


def _eid_value(eid):
    if hasattr(eid, "Value"):
        return int(eid.Value)
    return int(eid)


class _FakeSelection:
    def __init__(self, ids):
        self._ids = list(ids)

    def GetElementIds(self):
        return list(self._ids)


class _FakeUIDoc:
    def __init__(self, selected_ids=()):
        self.Selection = _FakeSelection([_FakeElementId(v) for v in selected_ids])


_OST_WALLS = -2000011
_OST_LINES = -2000051
_OST_TEXTNOTES = -2000300       # genuinely non-rescannable annotation.
_OST_DIMENSIONS = -2000280      # idem.


def test_active_selection_returns_empty_for_no_selection():
    kg = ProjectKG("p")
    result = kg_sync.active_selection_llm_ids(_FakeUIDoc([]), kg)
    assert result == ([], {}, False)


def test_active_selection_tolerates_null_uidoc():
    kg = ProjectKG("p")
    assert kg_sync.active_selection_llm_ids(None, kg) == ([], {}, False)


def test_active_selection_resolves_bound_element():
    kg = ProjectKG("p")
    level, _ = _seed(kg)
    kg_sync.bind(kg, level, 4242)
    llm_ids, unbound, refresh = kg_sync.active_selection_llm_ids(
        _FakeUIDoc([4242]), kg,
    )
    assert llm_ids == [level]
    assert unbound == {}
    assert refresh is False


def test_active_selection_unbound_without_doc_collapses_category():
    """No `doc` provided → category name falls back to '(inconnu)'."""
    kg = ProjectKG("p")
    level, _ = _seed(kg)
    kg_sync.bind(kg, level, 100)
    llm_ids, unbound, refresh = kg_sync.active_selection_llm_ids(
        _FakeUIDoc([100, 999, 888]), kg,
    )
    assert llm_ids == [level]
    assert unbound == {"(inconnu)": 2}
    assert refresh is False  # no Category info → can't claim rescannable.


def test_active_selection_categorises_unbound_with_doc():
    """When `doc` is provided, unbound elements get a category name and
    we flag refresh_actionable for categories `full_rescan` covers."""
    kg = ProjectKG("p")
    level, _ = _seed(kg)
    kg_sync.bind(kg, level, 100)

    # 100 → mapped Level. 200 → unbound Wall. 300 → unbound Detail Line
    # (both Walls and Lines are now in `_RESCANNABLE_CATEGORY_IDS` since
    # full_rescan scans them — selecting either should suggest a Refresh).
    doc = _FakeDoc({
        200: _FakeElement(_FakeCategory("Murs", _OST_WALLS)),
        300: _FakeElement(_FakeCategory("Lignes de détail", _OST_LINES)),
    })
    llm_ids, unbound, refresh = kg_sync.active_selection_llm_ids(
        _FakeUIDoc([100, 200, 300]), kg, doc=doc,
    )
    assert llm_ids == [level]
    assert unbound == {"Murs": 1, "Lignes de détail": 1}
    assert refresh is True


def test_active_selection_pure_non_rescannable_annotations_skip_refresh():
    """Text notes and dimensions aren't covered by `full_rescan` —
    a selection containing only those must NOT suggest a Refresh KG.
    Distinct from Lines (which Phase 13 added to the mapping)."""
    kg = ProjectKG("p")
    doc = _FakeDoc({
        500: _FakeElement(_FakeCategory("Notes textuelles", _OST_TEXTNOTES)),
        501: _FakeElement(_FakeCategory("Cotes", _OST_DIMENSIONS)),
    })
    _, unbound, refresh = kg_sync.active_selection_llm_ids(
        _FakeUIDoc([500, 501]), kg, doc=doc,
    )
    assert sum(unbound.values()) == 2
    assert refresh is False


def test_active_selection_lines_alone_suggest_refresh():
    """ModelCurves and DetailCurves are now covered by full_rescan
    (Phase 13). Selecting only lines should suggest a Refresh KG."""
    kg = ProjectKG("p")
    doc = _FakeDoc({
        600: _FakeElement(_FakeCategory("Lignes de modèle", _OST_LINES)),
    })
    _, unbound, refresh = kg_sync.active_selection_llm_ids(
        _FakeUIDoc([600]), kg, doc=doc,
    )
    assert unbound == {"Lignes de modèle": 1}
    assert refresh is True


def test_active_selection_preserves_order_of_bound_ids():
    kg = ProjectKG("p")
    level, wt = _seed(kg)
    kg_sync.bind(kg, level, 100)
    kg_sync.bind(kg, wt, 200)
    llm_ids, _, _ = kg_sync.active_selection_llm_ids(_FakeUIDoc([200, 100]), kg)
    assert llm_ids == [wt, level]


def test_active_selection_defends_against_category_lookup_errors():
    """A crash inside doc.GetElement(eid).Category must not abort the
    whole selection summary — the diagnostic helper should swallow it."""
    class _ExplodingDoc:
        def GetElement(self, eid):
            raise RuntimeError("ouch")

    kg = ProjectKG("p")
    _, unbound, refresh = kg_sync.active_selection_llm_ids(
        _FakeUIDoc([777]), kg, doc=_ExplodingDoc(),
    )
    # Element still counted, just under the fallback bucket.
    assert unbound == {"(inconnu)": 1}
    assert refresh is False


def test_llm_id_of_returns_bound_llm_id_or_none():
    kg = ProjectKG("p")
    level, wt = _seed(kg)
    kg_sync.bind(kg, level, 100)
    kg_sync.bind(kg, wt, 200)

    assert kg_sync.llm_id_of(kg, 100) == level
    assert kg_sync.llm_id_of(kg, 200) == wt

    class FakeElementId:
        Value = 999
    assert kg_sync.llm_id_of(kg, FakeElementId()) is None


# ----- @kg_synced decorator --------------------------------------------
#
# Monkeypatches `lib.revit_primitives.transaction` to a fake context manager
# so we never need an `Autodesk.Revit.DB.Document`. The decorator's lazy
# import inside `_wrap` resolves to whatever's on `sys.modules` at call
# time, so injecting a stub module works cleanly.


@pytest.fixture
def fake_revit_primitives(monkeypatch):
    """Inject a stub `lib.revit_primitives` with a tracking `transaction`."""
    calls = {"entered": 0, "committed": 0, "rolled_back": 0, "names": []}

    @contextmanager
    def fake_transaction(doc, name):
        calls["entered"] += 1
        calls["names"].append(name)
        try:
            yield
            calls["committed"] += 1
        except BaseException:
            calls["rolled_back"] += 1
            raise

    stub = types.ModuleType("lib.revit_primitives")
    stub.transaction = fake_transaction
    monkeypatch.setitem(sys.modules, "lib.revit_primitives", stub)
    yield calls


def test_kg_synced_commits_revit_and_persists_kg_on_success(tmp_path, fake_revit_primitives):
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    _seed(kg)
    kg.persist()

    @kg_sync.kg_synced("create_wall")
    def add_a_level(kg, doc):
        kg.add_node("Level", {"name": "N99", "elevation": 9.9})
        return "ok"

    result = add_a_level(kg, doc=object())
    assert result == "ok"
    assert fake_revit_primitives["committed"] == 1
    assert fake_revit_primitives["rolled_back"] == 0
    assert fake_revit_primitives["names"] == ["create_wall"]

    # KG persisted with the new level.
    loaded = ProjectKG.load(persist)
    assert "N99" in [loaded.get_node(nid)["name"] for nid in loaded.find_by_type("Level")]


def test_kg_synced_rolls_back_both_sides_on_exception(tmp_path, fake_revit_primitives):
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    _seed(kg)
    kg.persist()
    pre_levels = kg.find_by_type("Level")

    @kg_sync.kg_synced("boom_op")
    def explode(kg, doc):
        kg.add_node("Level", {"name": "ghost", "elevation": 1.0})
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        explode(kg, doc=object())

    # Revit side rolled back.
    assert fake_revit_primitives["rolled_back"] == 1
    assert fake_revit_primitives["committed"] == 0
    # KG side restored.
    assert kg.find_by_type("Level") == pre_levels
    # Disk unchanged (still the pre-rollback persist).
    loaded = ProjectKG.load(persist)
    assert loaded.find_by_type("Level") == pre_levels


def test_kg_synced_bare_decorator_uses_function_name(tmp_path, fake_revit_primitives):
    kg = ProjectKG("p", persist_path=tmp_path / "kg.json")
    _seed(kg)

    @kg_sync.kg_synced
    def do_a_thing(kg, doc):
        return 1

    do_a_thing(kg, doc=object())
    assert fake_revit_primitives["names"] == ["do_a_thing"]


# ----- project_id_for ---------------------------------------------------


def test_project_id_for_saved_doc_is_deterministic():
    doc1 = FakeDoc(path_name=r"C:\projects\MonProjet.rvt")
    doc2 = FakeDoc(path_name=r"C:\projects\MonProjet.rvt", title="something else")
    assert kg_sync.project_id_for(doc1) == kg_sync.project_id_for(doc2)
    assert len(kg_sync.project_id_for(doc1)) == kg_sync.PROJECT_ID_LEN


def test_project_id_for_different_paths_yield_different_ids():
    a = kg_sync.project_id_for(FakeDoc(path_name=r"C:\projects\A.rvt"))
    b = kg_sync.project_id_for(FakeDoc(path_name=r"C:\projects\B.rvt"))
    assert a != b


def test_project_id_for_unsaved_doc_falls_back_to_title():
    doc = FakeDoc(path_name="", title="Sandbox")
    pid = kg_sync.project_id_for(doc)
    assert len(pid) == kg_sync.PROJECT_ID_LEN
    # Same title → same id, irrespective of whitespace differences in PathName.
    same = FakeDoc(path_name="   ", title="Sandbox")
    assert kg_sync.project_id_for(same) == pid


# ----- open_or_create ---------------------------------------------------


def test_open_or_create_creates_empty_kg_with_persist_path(fake_home):
    doc = FakeDoc(path_name=r"C:\projects\Fresh.rvt")
    kg = kg_sync.open_or_create(doc)
    expected_path = config.kg_path_for(kg_sync.project_id_for(doc))
    assert kg.persist_path == expected_path
    assert kg.turn == 0
    assert kg.find_by_type("Level") == []
    # Nothing persisted yet — file is materialised only on first transaction.
    assert not expected_path.exists()


def test_open_or_create_loads_existing_kg(fake_home):
    doc = FakeDoc(path_name=r"C:\projects\Returning.rvt")
    pid = kg_sync.project_id_for(doc)
    path = config.kg_path_for(pid)
    path.parent.mkdir(parents=True, exist_ok=True)

    seed_kg = ProjectKG(project_id=pid, persist_path=path)
    seed_kg.advance_turn()
    seed_kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    seed_kg.persist()

    reloaded = kg_sync.open_or_create(doc)
    assert reloaded.project_id == pid
    assert reloaded.persist_path == path
    assert reloaded.turn == 1
    assert reloaded.find_by_type("Level")


# ----- full_rescan : llm_id stability + log filtering + FP rounding ----
#
# `full_rescan` was extended on 2026-05-12 (cf. JOURNAL.md entry) to:
# 1. Snapshot `{revit_id: llm_id}` BEFORE clearing topology, then reuse
#    those ids during the rebuild → walls/columns/etc. survive a refresh
#    without being renumbered (the UX bug that triggered this work).
# 2. Suppress per-element `create` events; only a single `rescan` log
#    event remains, with a summary including `preserved_llm_ids`.
# 3. Round float artifacts at the SI conversion boundary (6 decimals).
#
# A minimal `lib.revit_primitives` stub lets us exercise the orchestration
# without needing Autodesk.Revit.DB. We focus on Level (the simplest
# converter) — walls/columns/lines require deeper Revit stubs and are
# exercised at runtime in `refresh_kg.pushbutton`.


class _FakeLevel:
    """Stand-in for `Autodesk.Revit.DB.Level` — only the attrs the
    converter reads (`Name`, `Elevation`, `Id.Value`)."""
    def __init__(self, name, elevation, revit_id):
        self.Name = name
        self.Elevation = float(elevation)
        self.Id = _FakeElementId(revit_id)


def _install_rescan_stub(monkeypatch, *, levels=()):
    """Inject a `lib.revit_primitives` stub for full_rescan tests.

    Defaults: collectors return empty for everything except `levels`
    (focus on the simplest converter). `internal_to_meters` is identity
    so test elevations stay in the values we set. `transaction` is a
    no-op ctx. No `ensure_shared_param_binding` / `set_llm_id_on_element`
    on the stub → full_rescan's `getattr(...)` returns None → no
    Revit-side mirror is attempted (we're testing KG-side invariants).
    """
    stub = types.ModuleType("lib.revit_primitives")
    stub.levels = lambda doc: list(levels)
    stub.wall_types = lambda doc: []
    stub.walls = lambda doc: []
    stub.model_lines = lambda doc: []
    stub.detail_lines = lambda doc: []
    stub.column_types = lambda doc: []
    stub.columns = lambda doc: []
    stub.door_types = lambda doc: []
    stub.window_types = lambda doc: []
    stub.doors = lambda doc: []
    stub.windows = lambda doc: []
    stub.rooms = lambda doc: []
    stub.internal_to_meters = lambda x: float(x)

    @contextmanager
    def noop_tx(doc, name):
        yield

    stub.transaction = noop_tx
    monkeypatch.setitem(sys.modules, "lib.revit_primitives", stub)
    return stub


def test_full_rescan_reuses_llm_id_when_revit_id_matches(monkeypatch, tmp_path):
    """A Level whose revit_id was already in the KG keeps its llm_id
    across a rescan — the snapshot drives the reuse, no renumbering."""
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    kg.advance_turn()

    # Seed: two levels in the KG, bound to revit_ids 100 and 200.
    a = kg.add_node("Level", {"name": "old_A", "elevation": 0.0})
    b = kg.add_node("Level", {"name": "old_B", "elevation": 1.0})
    kg.set_revit_id(a, 100)
    kg.set_revit_id(b, 200)
    assert a == "level_001"
    assert b == "level_002"

    # Revit-side: same two levels (matching revit_ids) plus a new third
    # one (revit_id=300) that wasn't in the KG.
    levels = [
        _FakeLevel("A_new_name", 0.5, 100),  # match → keep llm_id `level_001`
        _FakeLevel("B_new_name", 1.5, 200),  # match → keep llm_id `level_002`
        _FakeLevel("brand_new", 2.0, 300),   # no match → fresh `level_003`
    ]
    _install_rescan_stub(monkeypatch, levels=levels)

    summary = kg_sync.full_rescan(doc=object(), kg=kg)

    # The two pre-existing llm_ids survive, pointing at the same revit_ids.
    assert kg.find_by_revit_id(100) == "level_001"
    assert kg.find_by_revit_id(200) == "level_002"
    # The new level gets the next counter slot, no collision.
    assert kg.find_by_revit_id(300) == "level_003"
    # Summary reports the reuse count.
    assert summary["levels"] == 3
    assert summary["preserved_llm_ids"] == 2
    # Names came through the converter (Level got *updated* on reuse —
    # the llm_id stays, but the attrs reflect the current Revit state).
    assert kg.get_node("level_001")["name"] == "A_new_name"


def test_full_rescan_action_log_has_rescan_only_no_creates(monkeypatch, tmp_path):
    """Per-element `create` events are suppressed during rescan — only a
    single `rescan` event with a summary should appear in the action log."""
    kg = ProjectKG("p", persist_path=tmp_path / "kg.json")
    kg.advance_turn()
    pre_log_len = len(kg.action_log)

    levels = [
        _FakeLevel("N00", 0.0, 100),
        _FakeLevel("N01", 3.0, 101),
        _FakeLevel("N02", 6.0, 102),
    ]
    _install_rescan_stub(monkeypatch, levels=levels)

    kg_sync.full_rescan(doc=object(), kg=kg)

    new_events = kg.action_log[pre_log_len:]
    # Exactly one event added, of type `rescan`.
    assert len(new_events) == 1
    assert new_events[0]["action"] == "rescan"
    # No `create` events sneaked in for the 3 levels.
    assert not any(e["action"] == "create" for e in new_events)
    # The rescan event carries the summary (levels count, etc.).
    assert new_events[0]["details"]["summary"]["levels"] == 3


def test_full_rescan_counter_advances_past_preserved_ids(monkeypatch, tmp_path):
    """When the snapshot reuses `level_001` (counter was at 1), and a new
    level is scanned, the next allocated id is `level_002`, not a
    collision. Counter preservation is what makes this work."""
    kg = ProjectKG("p", persist_path=tmp_path / "kg.json")
    kg.advance_turn()
    a = kg.add_node("Level", {"name": "A", "elevation": 0.0})
    kg.set_revit_id(a, 100)
    # Counter is at 1 — but soft-delete the only level so the snapshot
    # still preserves its mapping (verifies deleted ids don't break
    # counter logic either).
    kg.soft_delete(a)
    assert kg._counters["Level"] == 1  # noqa: SLF001

    levels = [
        _FakeLevel("A_back", 0.0, 100),   # match → reuse level_001
        _FakeLevel("fresh", 3.0, 999),    # no match → next counter slot
    ]
    _install_rescan_stub(monkeypatch, levels=levels)
    kg_sync.full_rescan(doc=object(), kg=kg)

    # level_001 is the reused one.
    assert kg.find_by_revit_id(100) == "level_001"
    # The fresh one is level_002 — counter advanced exactly by one.
    assert kg.find_by_revit_id(999) == "level_002"


# ----- FP rounding in converters ----------------------------------------


def test_r_strips_feet_to_meters_artifacts():
    """`_r` rounds at the SI conversion boundary so the KG JSON shows
    `0.2` instead of `0.20000000000000004` (post-2026-05-12 fix)."""
    # Classic artifacts observed in the validation runtime.
    assert kg_sync._r(0.20000000000000004) == 0.2  # noqa: SLF001
    assert kg_sync._r(4.999999999999992) == 5.0    # noqa: SLF001
    assert kg_sync._r(0.024999999999999998) == 0.025  # noqa: SLF001
    # Sub-micrometre precision preserved (6 decimals = 0.000001 m = 1 µm).
    assert kg_sync._r(1.234567891234) == 1.234568  # noqa: SLF001
    # Integers + clean values pass through unchanged.
    assert kg_sync._r(2.7) == 2.7  # noqa: SLF001
    assert kg_sync._r(0.0) == 0.0  # noqa: SLF001


def test_full_rescan_persists_clean_floats_in_kg(monkeypatch, tmp_path):
    """End-to-end: a Level with an `Elevation` that comes back from
    Revit as a float artifact lands in the KG as a clean rounded value."""
    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    kg.advance_turn()

    levels = [
        _FakeLevel("dirty", 0.20000000000000004, 100),
    ]
    _install_rescan_stub(monkeypatch, levels=levels)

    kg_sync.full_rescan(doc=object(), kg=kg)
    elevation = kg.get_node("level_001")["elevation"]
    assert elevation == 0.2
    # Re-load from disk to confirm round-trip persistence is also clean.
    reloaded = ProjectKG.load(persist)
    assert reloaded.get_node("level_001")["elevation"] == 0.2


# ----- refresh_node_from_revit + detect_drift ---------------------------


def test_refresh_node_from_revit_returns_none_for_unbound_node():
    """Pas de _revit_id sur le node → helper retourne None silencieusement
    (CLI / pytest path, le KG est déjà la source de vérité)."""
    kg = ProjectKG("p")
    nid = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    # No set_revit_id → unbound.
    result = kg_sync.refresh_node_from_revit(kg, doc=object(), llm_id=nid)
    assert result is None


def test_refresh_node_from_revit_returns_none_for_unknown_llm_id():
    """Helper tolère un llm_id absent (pas de KeyError) — pratique
    pour les tools qui itèrent sur une liste."""
    kg = ProjectKG("p")
    result = kg_sync.refresh_node_from_revit(
        kg, doc=object(), llm_id="ghost_001",
    )
    assert result is None


def test_detect_drift_no_drift_within_epsilon():
    """5e-4 m de tolérance pour absorber le round-trip pieds↔mètres."""
    drift, note = kg_sync.detect_drift(2.7, 2.70003)
    assert drift is False
    assert note is None


def test_detect_drift_scalar_reports_difference():
    drift, note = kg_sync.detect_drift(2.7, 2.95, field="height_m")
    assert drift is True
    assert "2.700" in note or "2.7" in note
    assert "2.950" in note or "2.95" in note
    assert "height_m" in note


def test_detect_drift_vector_compares_elementwise():
    """`[x, y]` p1 / p2 comparés composante par composante, max écart."""
    drift, note = kg_sync.detect_drift([0.0, 0.0], [0.0, 0.0001])
    assert drift is False
    drift, note = kg_sync.detect_drift([0.0, 0.0], [0.0, 0.05], field="p2")
    assert drift is True
    assert "p2" in note


def test_detect_drift_handles_none_silently():
    """Si on n'a pas pu relire le live (None) ou pas demandé (None), pas
    de drift signalé — pas de bruit."""
    drift, note = kg_sync.detect_drift(None, 2.0)
    assert drift is False
    drift, note = kg_sync.detect_drift(2.0, None)
    assert drift is False


def test_detect_drift_vector_length_mismatch_flags_drift():
    """Si Revit retourne un shape différent, c'est suspect → drift."""
    drift, note = kg_sync.detect_drift([0.0, 0.0], [0.0, 0.0, 0.0])
    assert drift is True
    assert "shape" in note


def test_kg_synced_propagates_revit_failure_and_restores_kg(tmp_path, monkeypatch):
    """If the Revit Tx fails on commit (raised by the fake), KG snapshot restored."""
    @contextmanager
    def angry_transaction(doc, name):
        try:
            yield
        finally:
            # Simulate a commit-time failure raised AFTER the body completed.
            # We raise from the finally so the body sees a clean execution
            # but the Tx exit still fails the whole stack.
            raise RuntimeError("commit failed")

    stub = types.ModuleType("lib.revit_primitives")
    stub.transaction = angry_transaction
    monkeypatch.setitem(sys.modules, "lib.revit_primitives", stub)

    kg = ProjectKG("p", persist_path=tmp_path / "kg.json")
    _seed(kg)
    pre_levels = kg.find_by_type("Level")

    @kg_sync.kg_synced("late_failure")
    def add_then_fail(kg, doc):
        kg.add_node("Level", {"name": "midair", "elevation": 1.0})

    with pytest.raises(RuntimeError, match="commit failed"):
        add_then_fail(kg, doc=object())

    # The body added a level, but the outer KG transaction caught the
    # commit-time exception and restored the snapshot. So the level is gone.
    assert kg.find_by_type("Level") == pre_levels


# ----- consume_pending_diffs (auto-sync hook consumer) ------------------
#
# Mocks `refresh_node_from_revit` to avoid pulling on `Autodesk.Revit.DB`
# (the real implementation does `doc.GetElement(ElementId(raw))`). We just
# track which llm_ids were refreshed, which is enough to assert behavior.


@pytest.fixture
def fake_refresh(monkeypatch):
    calls = []

    def _fake(kg, doc, llm_id):
        calls.append(llm_id)
        # Mimic the real "returns dict on success, None on no binding".
        return {"refreshed": True}

    monkeypatch.setattr(kg_sync, "refresh_node_from_revit", _fake)
    return calls


def _seed_bound_wall(kg: ProjectKG, revit_id: int = 5001) -> str:
    """Add a Wall node with a Revit binding and return its llm_id."""
    kg.advance_turn()
    level = kg.add_node("Level", {"name": "N00", "elevation": 0.0})
    wt = kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})
    wall = kg.add_node("Wall", {
        "type_ref": wt, "level_ref": level,
        "p1": [0.0, 0.0], "p2": [3.0, 0.0],
        "length": 3.0, "height": 3.0,
    })
    kg.set_revit_id(wall, revit_id)
    return wall


def test_consume_pending_diffs_empty_buffer_returns_zero_counts(fake_home, fake_refresh):
    kg = ProjectKG("p-empty", persist_path=fake_home / "p-empty.kg.json")
    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    assert summary["ok"] is True
    assert summary["records"] == 0
    assert summary["modified_applied"] == 0
    assert summary["deleted_applied"] == 0
    assert fake_refresh == []


def _write_diffs(project_id: str, lines: list) -> Path:
    """Write JSONL records to the project's pending_diffs buffer."""
    import json as _j
    path = config.pending_diffs_path_for(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(_j.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


def test_consume_pending_diffs_modified_applies_refresh_when_bound(fake_home, fake_refresh):
    kg = ProjectKG("p-mod", persist_path=fake_home / "p-mod.kg.json")
    wall = _seed_bound_wall(kg, revit_id=5001)

    _write_diffs("p-mod", [
        {"ts": "2026-05-14T10:00:00Z", "tx_names": ["MoveWall"],
         "added": [], "modified": [5001], "deleted": []},
    ])

    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    assert summary["ok"] is True
    assert summary["records"] == 1
    assert summary["modified_applied"] == 1
    assert summary["skipped_unbound"] == 0
    assert fake_refresh == [wall]


def test_consume_pending_diffs_modified_skips_unbound_ids(fake_home, fake_refresh):
    kg = ProjectKG("p-mod-unbound", persist_path=fake_home / "p-mod-unbound.kg.json")
    _seed_bound_wall(kg, revit_id=5001)

    _write_diffs("p-mod-unbound", [
        {"ts": "x", "tx_names": ["t"], "added": [],
         "modified": [9999], "deleted": []},  # 9999 is not in KG
    ])

    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    assert summary["modified_applied"] == 0
    assert summary["skipped_unbound"] == 1
    assert fake_refresh == []


def test_consume_pending_diffs_deleted_soft_deletes_bound_nodes(fake_home, fake_refresh):
    kg = ProjectKG("p-del", persist_path=fake_home / "p-del.kg.json")
    wall = _seed_bound_wall(kg, revit_id=5001)
    assert kg.get_node(wall).get("deleted_at_turn") is None

    _write_diffs("p-del", [
        {"ts": "x", "tx_names": ["DeleteWall"], "added": [],
         "modified": [], "deleted": [5001]},
    ])

    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    assert summary["deleted_applied"] == 1
    assert kg.get_node(wall).get("deleted_at_turn") is not None


def test_consume_pending_diffs_added_ids_counted_as_skipped(fake_home, fake_refresh):
    kg = ProjectKG("p-add", persist_path=fake_home / "p-add.kg.json")
    _seed_bound_wall(kg, revit_id=5001)

    _write_diffs("p-add", [
        {"ts": "x", "tx_names": ["UserCreatedWall"],
         "added": [7777, 8888], "modified": [], "deleted": []},
    ])

    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    assert summary["skipped_added"] == 2
    assert summary["modified_applied"] == 0
    assert summary["deleted_applied"] == 0


def test_consume_pending_diffs_truncates_buffer_on_success(fake_home, fake_refresh):
    kg = ProjectKG("p-trunc", persist_path=fake_home / "p-trunc.kg.json")
    _seed_bound_wall(kg, revit_id=5001)
    buf = _write_diffs("p-trunc", [
        {"ts": "x", "tx_names": ["t"], "added": [],
         "modified": [5001], "deleted": []},
    ])
    assert buf.exists()
    kg_sync.consume_pending_diffs(kg, doc=object())
    assert not buf.exists()


def test_consume_pending_diffs_idempotent_on_already_deleted(fake_home, fake_refresh):
    kg = ProjectKG("p-idem", persist_path=fake_home / "p-idem.kg.json")
    wall = _seed_bound_wall(kg, revit_id=5001)
    # Soft-delete it first.
    kg.soft_delete(wall)

    _write_diffs("p-idem", [
        {"ts": "x", "tx_names": ["t"], "added": [],
         "modified": [], "deleted": [5001]},
    ])
    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    # Already-deleted is a no-op (not double-deleted, not counted).
    assert summary["deleted_applied"] == 0


def test_consume_pending_diffs_skips_malformed_lines(fake_home, fake_refresh):
    kg = ProjectKG("p-malformed", persist_path=fake_home / "p-malformed.kg.json")
    _seed_bound_wall(kg, revit_id=5001)

    # Write a mix of valid + malformed JSON to test resilience.
    path = config.pending_diffs_path_for("p-malformed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"ts":"x","tx_names":["t"],"added":[],"modified":[5001],"deleted":[]}\n'
        '{not valid json\n'
        '{"ts":"y","tx_names":["t"],"added":[],"modified":[],"deleted":[5001]}\n',
        encoding="utf-8",
    )
    summary = kg_sync.consume_pending_diffs(kg, doc=object())
    # 2 valid records (1 modified + 1 deleted), 1 malformed skipped silently.
    assert summary["records"] == 2
    assert summary["modified_applied"] == 1
    assert summary["deleted_applied"] == 1
