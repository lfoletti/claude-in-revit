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
