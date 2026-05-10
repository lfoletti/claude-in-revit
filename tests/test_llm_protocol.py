"""Tests for lib.llm_protocol — docstring parsing, schema, registry, dispatcher."""
from __future__ import annotations

import json
from typing import List, Optional

import pytest

from lib import llm_protocol
from lib.project_kg import ProjectKG


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets a clean registry — tools register on import otherwise."""
    llm_protocol.reset_registry()
    yield
    llm_protocol.reset_registry()


# ----- Docstring parsing ------------------------------------------------

def test_parse_docstring_extracts_all_sections():
    doc = """Modifie la hauteur d'allège des fenêtres.

    Concepts: ouverture, fenêtre, allège
    Phrases: "lève les allèges", "passe les fenêtres à X cm"
    Similar: change_param, set_lintel_height

    Args:
        filter: critères de sélection
        new_height_mm: nouvelle hauteur en mm

    Returns:
        {"ok": bool}
    """
    meta = llm_protocol.parse_docstring(doc)
    assert meta["description"] == "Modifie la hauteur d'allège des fenêtres."
    assert meta["concepts"] == ["ouverture", "fenêtre", "allège"]
    assert meta["phrases"] == ["lève les allèges", "passe les fenêtres à X cm"]
    assert meta["similar"] == ["change_param", "set_lintel_height"]
    assert meta["args"] == {
        "filter": "critères de sélection",
        "new_height_mm": "nouvelle hauteur en mm",
    }
    assert "ok" in meta["returns"]


def test_parse_docstring_handles_missing_sections():
    meta = llm_protocol.parse_docstring("Just a description.")
    assert meta["description"] == "Just a description."
    assert meta["concepts"] == []
    assert meta["args"] == {}


def test_parse_docstring_handles_empty():
    meta = llm_protocol.parse_docstring(None)
    assert meta["description"] == ""
    assert meta["concepts"] == []


# ----- Tool registration & schema --------------------------------------

def test_tool_registers_and_generates_schema():
    @llm_protocol.tool(name="demo_create", tier=1)
    def demo_create(
        kg: ProjectKG,
        name: str,
        count: int = 1,
        tags: Optional[List[str]] = None,
    ) -> dict:
        """Crée un truc.

        Args:
            name: nom du truc
            count: nombre de copies
            tags: étiquettes optionnelles
        """
        return {"ok": True}

    registry = llm_protocol._REGISTRY  # noqa: SLF001
    entry = registry["demo_create"]
    assert entry.tier == 1
    schema = entry.input_schema
    assert schema["type"] == "object"
    props = schema["properties"]
    assert props["name"] == {"type": "string", "description": "nom du truc"}
    assert props["count"] == {"type": "integer", "description": "nombre de copies"}
    assert props["tags"]["type"] == "array"
    # required = no default and not Optional
    assert schema["required"] == ["name"]


def test_tool_rejects_function_without_kg_first_param():
    with pytest.raises(TypeError, match="first param must be named 'kg'"):
        @llm_protocol.tool(name="bad")
        def bad(x: int) -> dict:  # noqa: ARG001  - intentional bad signature
            """Bad."""
            return {}


def test_tools_as_anthropic_payload_filters_by_tier():
    @llm_protocol.tool(name="t1", tier=1)
    def t1(kg: ProjectKG) -> dict:
        """T1."""
        return {}

    @llm_protocol.tool(name="t2", tier=2)
    def t2(kg: ProjectKG) -> dict:
        """T2."""
        return {}

    payload = llm_protocol.tools_as_anthropic_payload(tier_max=1)
    names = [t["name"] for t in payload]
    assert names == ["t1"]

    payload_all = llm_protocol.tools_as_anthropic_payload()
    names_all = sorted(t["name"] for t in payload_all)
    assert names_all == ["t1", "t2"]


# ----- Dispatcher -------------------------------------------------------

def test_dispatch_runs_tool_and_returns_result():
    @llm_protocol.tool(name="demo_create")
    def demo_create(kg: ProjectKG, name: str) -> dict:
        """Demo."""
        return {"echoed": name}

    kg = ProjectKG("p")
    result = llm_protocol.dispatch_tool_use(
        tool_name="demo_create",
        tool_input={"name": "hi"},
        tool_use_id="toolu_1",
        kg=kg,
    )
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "toolu_1"
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload == {"echoed": "hi"}


def test_dispatch_unknown_tool_returns_is_error():
    kg = ProjectKG("p")
    result = llm_protocol.dispatch_tool_use(
        tool_name="does_not_exist",
        tool_input={},
        tool_use_id="toolu_1",
        kg=kg,
    )
    assert result["is_error"] is True
    assert "Unknown tool" in result["content"]


def test_dispatch_rolls_back_kg_on_tool_exception(tmp_path):
    @llm_protocol.tool(name="boom")
    def boom_tool(kg: ProjectKG, name: str) -> dict:
        """Mutates KG then raises."""
        kg.add_node("Level", {"name": name, "elevation": 0.0})
        raise RuntimeError("explode")

    persist = tmp_path / "kg.json"
    kg = ProjectKG("p", persist_path=persist)
    kg.advance_turn()
    kg.add_node("Level", {"name": "before", "elevation": 0.0})
    kg.persist()
    pre_levels = kg.find_by_type("Level")

    result = llm_protocol.dispatch_tool_use(
        tool_name="boom",
        tool_input={"name": "should_disappear"},
        tool_use_id="toolu_2",
        kg=kg,
    )
    assert result["is_error"] is True
    assert "explode" in result["content"]
    # KG was rolled back — only the pre-existing Level remains
    assert kg.find_by_type("Level") == pre_levels


def test_dispatch_serializes_non_string_results_via_json():
    @llm_protocol.tool(name="returns_dict")
    def returns_dict(kg: ProjectKG) -> dict:
        """Returns nested dict with non-ascii."""
        return {"ok": True, "héro": [1, 2, 3]}

    kg = ProjectKG("p")
    result = llm_protocol.dispatch_tool_use(
        tool_name="returns_dict",
        tool_input={},
        tool_use_id="toolu_1",
        kg=kg,
    )
    assert result["is_error"] is False
    payload = json.loads(result["content"])
    assert payload == {"ok": True, "héro": [1, 2, 3]}
