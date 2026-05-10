"""project_kg.py — NetworkX-backed Knowledge Graph of the Revit project state.

V0 scope: typed nodes/edges, lifecycle attrs (created_at_turn / modified_at_turn /
deleted_at_turn), JSON persistence, atomic transactions via snapshot+restore.

Revit binding (kg_sync.py, the @kg_synced decorator that wraps Revit transactions)
is NOT in scope here — the slice exercises mutations against the KG only.
"""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

import networkx as nx


# Schema declared as required/optional attribute sets per node type.
# Subset of §4.1 of the design doc — extend here when new types are needed.
NODE_TYPES: Dict[str, Dict[str, Set[str]]] = {
    "Level": {
        "required": {"name", "elevation"},
        "optional": set(),
    },
    "Wall": {
        "required": {"type_ref", "level_ref", "p1", "p2", "length", "height"},
        "optional": set(),
    },
    "Door": {
        "required": {"type_ref", "host_wall_ref", "position", "sill_height", "head_height"},
        "optional": set(),
    },
    "Window": {
        "required": {"type_ref", "host_wall_ref", "position", "sill_height", "head_height"},
        "optional": set(),
    },
    "Room": {
        "required": {"name", "level_ref"},
        "optional": {"area", "boundary_walls", "use_subcategory"},
    },
    "WallType": {
        "required": {"name", "total_thickness"},
        "optional": {"layers_summary"},
    },
    "FamilyType": {
        "required": {"family_name", "type_name"},
        "optional": {"dimensions"},
    },
}

# Allowed edge types — used as the MultiDiGraph key, so at most one edge of
# each type between a given (src, dst) pair.
EDGE_TYPES: Set[str] = {
    "at_level",       # Wall/Door/Window/Room -> Level
    "is_type",        # Wall -> WallType, Door/Window -> FamilyType
    "hosts",          # Wall -> Door/Window
    "bounded_by",     # Room -> Wall (one per boundary wall)
    "connects_at",    # Wall -> Wall (corner/T/cross via attrs)
    "derived_from",   # Element -> Element (lineage)
}

# Lifecycle attribute names — centralised so callers stay consistent.
CREATED_AT = "created_at_turn"
MODIFIED_AT = "modified_at_turn"  # list[int]
DELETED_AT = "deleted_at_turn"    # int or None

_RESERVED_ATTRS: Set[str] = {"_type", CREATED_AT, MODIFIED_AT, DELETED_AT}


class ProjectKG:
    """Typed graph of Revit elements with action-grained history.

    A single MultiDiGraph carries every project element. Nodes have a `_type`
    attribute (one of NODE_TYPES) plus lifecycle attrs. Mutations should go
    through `transaction()`, which snapshots state on entry and restores it
    on exception.
    """

    def __init__(self, project_id: str, persist_path: Optional[Path] = None) -> None:
        self.project_id = project_id
        self.persist_path = persist_path
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()
        self._turn: int = 0
        self._action_log: List[Dict[str, Any]] = []
        self._counters: Dict[str, int] = {}

    # ----- Turn counter -------------------------------------------------

    @property
    def turn(self) -> int:
        return self._turn

    def advance_turn(self) -> int:
        self._turn += 1
        return self._turn

    # ----- llm_id allocation -------------------------------------------

    def _next_llm_id(self, node_type: str) -> str:
        self._counters[node_type] = self._counters.get(node_type, 0) + 1
        return "{}_{:03d}".format(node_type.lower(), self._counters[node_type])

    # ----- Node operations ---------------------------------------------

    def add_node(
        self,
        node_type: str,
        attrs: Dict[str, Any],
        llm_id: Optional[str] = None,
    ) -> str:
        if node_type not in NODE_TYPES:
            raise ValueError("Unknown node type: {}".format(node_type))
        spec = NODE_TYPES[node_type]
        keys = set(attrs)
        missing = spec["required"] - keys
        if missing:
            raise ValueError(
                "Missing required attrs for {}: {}".format(node_type, sorted(missing))
            )
        unknown = keys - spec["required"] - spec["optional"]
        if unknown:
            raise ValueError(
                "Unknown attrs for {}: {}".format(node_type, sorted(unknown))
            )

        if llm_id is None:
            llm_id = self._next_llm_id(node_type)
        if llm_id in self._g:
            raise ValueError("llm_id already in graph: {}".format(llm_id))

        full_attrs: Dict[str, Any] = dict(attrs)
        full_attrs["_type"] = node_type
        full_attrs[CREATED_AT] = self._turn
        full_attrs[MODIFIED_AT] = []
        full_attrs[DELETED_AT] = None

        self._g.add_node(llm_id, **full_attrs)
        self._log("create", llm_id, node_type=node_type, attrs=dict(attrs))
        return llm_id

    def modify_node(self, llm_id: str, updates: Dict[str, Any]) -> None:
        if llm_id not in self._g:
            raise KeyError(llm_id)
        node = self._g.nodes[llm_id]
        if node.get(DELETED_AT) is not None:
            raise ValueError("Node {} is soft-deleted".format(llm_id))

        node_type = node["_type"]
        spec = NODE_TYPES[node_type]
        update_keys = set(updates)
        unknown = update_keys - spec["required"] - spec["optional"]
        if unknown:
            raise ValueError(
                "Unknown attrs for {}: {}".format(node_type, sorted(unknown))
            )

        before = {k: node.get(k) for k in update_keys}
        node.update(updates)
        node[MODIFIED_AT] = list(node.get(MODIFIED_AT, [])) + [self._turn]
        self._log("modify", llm_id, before=before, after=dict(updates))

    def soft_delete(self, llm_id: str) -> None:
        if llm_id not in self._g:
            raise KeyError(llm_id)
        node = self._g.nodes[llm_id]
        if node.get(DELETED_AT) is not None:
            return
        node[DELETED_AT] = self._turn
        self._log("delete", llm_id)

    # ----- Edge operations ---------------------------------------------

    def add_edge(
        self,
        src: str,
        dst: str,
        edge_type: str,
        **attrs: Any,
    ) -> None:
        if edge_type not in EDGE_TYPES:
            raise ValueError("Unknown edge type: {}".format(edge_type))
        if src not in self._g or dst not in self._g:
            raise KeyError(
                "Edge endpoints must exist: {} -> {}".format(src, dst)
            )
        self._g.add_edge(src, dst, key=edge_type, _type=edge_type, **attrs)

    # ----- Queries ------------------------------------------------------

    def has_node(self, llm_id: str) -> bool:
        return llm_id in self._g

    def get_node(self, llm_id: str) -> Dict[str, Any]:
        if llm_id not in self._g:
            raise KeyError(llm_id)
        return dict(self._g.nodes[llm_id])

    def find_by_type(
        self,
        node_type: str,
        include_deleted: bool = False,
    ) -> List[str]:
        out: List[str] = []
        for nid, attrs in self._g.nodes(data=True):
            if attrs.get("_type") != node_type:
                continue
            if not include_deleted and attrs.get(DELETED_AT) is not None:
                continue
            out.append(nid)
        return out

    def find_by_name(
        self,
        name: str,
        node_type: Optional[str] = None,
        include_deleted: bool = False,
    ) -> List[str]:
        """Match nodes whose `name` attribute equals `name` (case-sensitive)."""
        out: List[str] = []
        for nid, attrs in self._g.nodes(data=True):
            if node_type is not None and attrs.get("_type") != node_type:
                continue
            if not include_deleted and attrs.get(DELETED_AT) is not None:
                continue
            if attrs.get("name") == name:
                out.append(nid)
        return out

    def count_by_type(
        self,
        node_type: str,
        include_deleted: bool = False,
    ) -> int:
        return len(self.find_by_type(node_type, include_deleted))

    # ----- Action log ---------------------------------------------------

    def _log(self, action: str, target: str, **details: Any) -> None:
        self._action_log.append({
            "turn": self._turn,
            "action": action,
            "target": target,
            "details": details,
        })

    @property
    def action_log(self) -> List[Dict[str, Any]]:
        return list(self._action_log)

    def diff_since(self, since_turn: int) -> List[Dict[str, Any]]:
        return [a for a in self._action_log if a["turn"] >= since_turn]

    # ----- Serialization -----------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "turn": self._turn,
            "counters": dict(self._counters),
            "nodes": [
                {"id": nid, **dict(attrs)}
                for nid, attrs in self._g.nodes(data=True)
            ],
            "edges": [
                {"src": u, "dst": v, "key": k, **dict(attrs)}
                for u, v, k, attrs in self._g.edges(keys=True, data=True)
            ],
            "action_log": list(self._action_log),
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        persist_path: Optional[Path] = None,
    ) -> "ProjectKG":
        kg = cls(project_id=data["project_id"], persist_path=persist_path)
        kg._turn = int(data.get("turn", 0))
        kg._counters = dict(data.get("counters", {}))
        for n in data.get("nodes", []):
            attrs = dict(n)
            nid = attrs.pop("id")
            kg._g.add_node(nid, **attrs)
        for e in data.get("edges", []):
            attrs = dict(e)
            u = attrs.pop("src")
            v = attrs.pop("dst")
            k = attrs.pop("key")
            kg._g.add_edge(u, v, key=k, **attrs)
        kg._action_log = list(data.get("action_log", []))
        return kg

    def persist(self) -> None:
        if self.persist_path is None:
            return
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        with self.persist_path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)

    @classmethod
    def load(cls, persist_path: Path) -> "ProjectKG":
        with persist_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data, persist_path=persist_path)

    # ----- Transactions -------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["ProjectKG"]:
        """Atomic mutation block. Restores prior state on any exception.

        Persists to disk on success.
        """
        snapshot = copy.deepcopy(self.to_dict())
        try:
            yield self
        except BaseException:
            restored = ProjectKG.from_dict(snapshot, persist_path=self.persist_path)
            self._g = restored._g
            self._turn = restored._turn
            self._counters = restored._counters
            self._action_log = restored._action_log
            raise
        else:
            self.persist()
