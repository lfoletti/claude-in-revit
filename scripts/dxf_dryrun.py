"""scripts/dxf_dryrun.py — Offline replay of the DXF import pipeline.

Run Phase 1 + Phase 2 of the DXF import flow with `doc=None`, against an
in-memory ProjectKG. Mimics what the LLM orchestrates in a real Revit
session, minus the Revit transactions. All KG mutations land in memory ;
the resulting state plus per-tool returned payloads are dumped to JSON
for golden-diffing.

Usage::

    python -m scripts.dxf_dryrun --project-dir tests/fixtures/P7
    python -m scripts.dxf_dryrun --project-dir <path> --out report.json

Exit codes: 0 success, 1 unexpected error, 2 a Phase tool raised.

Limitations :
- No Revit means no geometry sanity check — Revit might still reject what
  we'd create (oversize openings beyond pre-checks, malformed boundaries).
- `kg_sync.refresh_node_from_revit` is bypassed in the `doc=None` path,
  so the KG holds the *requested* geometry, not the post-snap geometry
  Revit would settle on.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

# sys.path fixup — mirror scripts/cli.py. `lib/` lives inside the PyRevit
# extension bundle ; insert that directory so `from lib import ...` resolves
# identically to the runtime.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_HOST = _REPO_ROOT / "claude-in-revit.extension"
if str(_LIB_HOST) not in sys.path:
    sys.path.insert(0, str(_LIB_HOST))


def _bootstrap_kg(project_id: str = "dxf-dryrun"):
    """Empty in-memory KG — no persistence."""
    from lib.project_kg import ProjectKG
    return ProjectKG(project_id=project_id, persist_path=None)


def _kg_seed_levels_from_reconcile(kg, reconcile_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mimic what the LLM would do after `levels_reconcile_with_dxf` :
    create the missing levels via `levels_create_many` (doc=None path).

    We take the coupe-side levels as ground truth (it's a fresh KG without
    Revit levels to reconcile against). Returns the created entries for
    inclusion in the dump.
    """
    from lib.tools import levels as levels_tool

    # When the project KG starts empty, reconcile returns coupe levels in
    # `missing_in_project` (everything is missing). We use those.
    coupe_levels: List[Dict[str, Any]] = list(
        reconcile_payload.get("missing_in_project") or []
    )
    if not coupe_levels:
        # Fallback: caller already has levels in KG, nothing to seed.
        return []

    items = [
        # reconcile payload uses `elevation_m` ; so does levels.create_many.
        {"name": lv["name"], "elevation_m": float(lv["elevation_m"])}
        for lv in coupe_levels
    ]
    levels_tool.create_many(kg=kg, doc=None, items=items)

    out: List[Dict[str, Any]] = []
    for lid in kg.find_by_type("Level"):
        node = kg.get_node(lid)
        out.append({
            "llm_id": lid,
            "name": node["name"],
            "elevation": node["elevation"],
        })
    out.sort(key=lambda r: r["elevation"])
    return out


def _kg_seed_dxf_context(kg, *, directory: str, section_line_specs, linked_view_specs):
    """Mimic the LLM-orchestrated `dxf_context_register_*_many` calls
    after Phase 1 markers/assignment."""
    from lib.tools import dxf_context

    if section_line_specs:
        dxf_context.register_section_line_many(
            kg=kg, section_lines=section_line_specs,
        )
    if linked_view_specs:
        dxf_context.register_linked_view_many(
            kg=kg, entries=linked_view_specs,
        )


def _fake_link_specs_for_dryrun(plans: List[Path], coupes: List[Path], elevs: List[Path]) -> List[Dict[str, Any]]:
    """Synthesize `linked_view_specs` for the dryrun.

    The real `views_link_cad_many` returns Revit `link_revit_id` + a Revit
    `view_revit_id` per (DXF, view). We don't have those offline, so we
    mint synthetic positive ints (registry validates ``> 0``, otherwise
    is opaque to the integers themselves) and inject `view_kind` /
    `view_name` matching the file name convention so downstream tools can
    map `plan_path → level` via name.
    """
    entries: List[Dict[str, Any]] = []
    rid = 1_000_000
    for p in plans:
        # "Projet8-Plan d'étage - Niveau 0.dxf" → "Niveau 0"
        # Convention : view_name = last token of "Plan d'étage - <name>".
        stem = p.stem
        sep = " - "
        view_name = stem.split(sep)[-1] if sep in stem else stem
        entries.append({
            "file_path": str(p),
            "link_revit_id": rid,
            "view_revit_id": rid + 1,
            "view_kind": "plan",
            "view_name": view_name,
        })
        rid += 2
    for c in coupes:
        stem = c.stem
        view_name = stem.split(" - ")[-1] if " - " in stem else stem
        entries.append({
            "file_path": str(c),
            "link_revit_id": rid,
            "view_revit_id": rid + 1,
            "view_kind": "section",
            "view_name": view_name,
        })
        rid += 2
    for e in elevs:
        stem = e.stem
        view_name = stem.split(" - ")[-1] if " - " in stem else stem
        entries.append({
            "file_path": str(e),
            "link_revit_id": rid,
            "view_revit_id": rid + 1,
            "view_kind": "elevation",
            "view_name": view_name,
        })
        rid += 2
    return entries


def _classify_dxfs(project_dir: Path):
    """Triage the *.dxf files in `project_dir` by kind (plan / section /
    elevation) using filename heuristics. Returns three lists of `Path`.
    """
    plans: List[Path] = []
    coupes: List[Path] = []
    elevs: List[Path] = []
    for p in sorted(project_dir.glob("*.dxf")):
        name = p.name.lower()
        if "plan d'étage" in name or "plan d'etage" in name:
            plans.append(p)
        elif "elévation" in name or "élévation" in name or "elevation" in name:
            elevs.append(p)
        elif "coupe" in name:
            coupes.append(p)
    return plans, coupes, elevs


def _dump_kg(kg) -> Dict[str, Any]:
    """Project-shape KG snapshot for the JSON dump.

    Strips lifecycle attrs (created_at_turn etc.) for stability — the
    golden focuses on what was *modeled*, not when it happened.
    """
    def _node_view(nid: str) -> Dict[str, Any]:
        node = kg.get_node(nid)
        return {
            k: v for k, v in node.items()
            if not k.startswith("_") and k not in {
                "created_at_turn", "modified_at_turn", "deleted_at_turn",
            }
        }

    out: Dict[str, List[Dict[str, Any]]] = {}
    for node_type in (
        "Level", "WallType", "Wall", "FamilyType",
        "Window", "Door", "FloorType", "Floor",
    ):
        ids = sorted(kg.find_by_type(node_type))
        out[node_type] = [{"llm_id": nid, **_node_view(nid)} for nid in ids]
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir", default="tests/fixtures/P7",
        help="Directory of DXFs to import (plan + sections + elevations).",
    )
    parser.add_argument(
        "--out", default=None,
        help="Output JSON path. Default : <project_dir>/dryrun_output.json.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print per-phase progress to stderr.",
    )
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print("Not a directory: {}".format(project_dir), file=sys.stderr)
        return 1
    out_path = Path(args.out) if args.out else project_dir / "dryrun_output.json"

    plans, coupes, elevs = _classify_dxfs(project_dir)
    if args.verbose:
        print("[dryrun] {} plans / {} coupes / {} élévations".format(
            len(plans), len(coupes), len(elevs)), file=sys.stderr)
    if not plans or not coupes:
        print("Need at least one plan and one section in {}".format(project_dir),
              file=sys.stderr)
        return 1

    kg = _bootstrap_kg()
    report: Dict[str, Any] = {
        "project_dir": str(project_dir),
        "files": {
            "plans": [str(p) for p in plans],
            "coupes": [str(c) for c in coupes],
            "elevations": [str(e) for e in elevs],
        },
    }

    # Tool callers — imported lazily so the sys.path fixup at module top
    # has taken effect.
    from lib.tools import dwg_import

    # ---- Phase 1 : check + inspect + markers + assign ---------------
    if args.verbose:
        print("[dryrun] Phase 1 — check_planset_integrity", file=sys.stderr)
    report["check_planset_integrity"] = dwg_import.check_planset_integrity(
        kg=kg, directory=str(project_dir),
    )

    if args.verbose:
        print("[dryrun] Phase 1 — inspect_sections", file=sys.stderr)
    report["inspect_sections"] = dwg_import.inspect_sections(
        kg=kg, directory=str(project_dir),
    )

    if args.verbose:
        print("[dryrun] Phase 1 — find_section_markers", file=sys.stderr)
    # find_section_markers takes a single plan ; we pick the first.
    plan_for_markers = str(plans[0])
    markers_payload = dwg_import.find_section_markers(
        kg=kg, file_path=plan_for_markers,
    )
    report["find_section_markers"] = markers_payload

    if args.verbose:
        print("[dryrun] Phase 1 — assign_coupes_to_traits", file=sys.stderr)
    section_markers = markers_payload.get("markers") or []
    if section_markers and len(coupes) == len(section_markers):
        report["assign_coupes_to_traits"] = dwg_import.assign_coupes_to_traits(
            kg=kg, coupe_paths=[str(c) for c in coupes],
            section_markers=section_markers,
        )
    else:
        report["assign_coupes_to_traits"] = {
            "skipped": True,
            "reason": "marker count {} != coupe count {}".format(
                len(section_markers), len(coupes)),
        }

    # Pick a coupe for level reconciliation (any will do — they share levels).
    if args.verbose:
        print("[dryrun] Phase 1 — levels_reconcile_with_dxf", file=sys.stderr)
    from lib.tools import levels as levels_tool
    reconcile = levels_tool.reconcile_with_dxf(kg=kg, coupe_path=str(coupes[0]))
    report["levels_reconcile_with_dxf"] = reconcile

    # ---- Phase 1.5 : seed KG (mimic LLM-orchestrated mutations) ----
    levels_created = _kg_seed_levels_from_reconcile(kg, reconcile)
    report["kg_seed"] = {"levels_created": levels_created}

    # Build section_line_specs from assignment + markers. The assignment
    # entry only carries `marker_index` ; the geometry lives in the
    # original marker payload.
    section_line_specs: List[Dict[str, Any]] = []
    assignment = report["assign_coupes_to_traits"]
    if not assignment.get("skipped"):
        for entry in assignment.get("assignment") or []:
            mk = section_markers[entry["marker_index"]]
            view_dir = (
                mk.get("inferred_view_dir")
                or (mk.get("view_dir_candidates") or ["up"])[0]
            )
            section_line_specs.append({
                "coupe_path": entry["coupe_path"],
                "plan_p1": mk["p1_m"],
                "plan_p2": mk["p2_m"],
                "view_dir": view_dir,
                "name": Path(entry["coupe_path"]).stem,
                "confirmed_by_user": True,
                "scale_verified": True,
                "drift_pct": entry.get("drift_pct", 0.0),
            })

    linked_view_specs = _fake_link_specs_for_dryrun(plans, coupes, elevs)
    _kg_seed_dxf_context(
        kg, directory=str(project_dir),
        section_line_specs=section_line_specs,
        linked_view_specs=linked_view_specs,
    )

    # Materialise the inspection in the DxfImportContext so collectors
    # that iterate `ctx["files"]` (e.g. `_collect_plan_openings_world`)
    # see the plans. In a real Revit session the LLM calls
    # `dxf_context_register_inspection` right after `dwg_inspect_sections`.
    from lib.tools import dxf_context as _dxf_context_mod
    _dxf_context_mod.register_inspection(
        kg=kg, directory=str(project_dir),
        inspection=report["inspect_sections"],
    )

    report["kg_seed"]["section_lines"] = len(section_line_specs)
    report["kg_seed"]["linked_views"] = len(linked_view_specs)
    report["kg_seed"]["files_registered"] = len(report["inspect_sections"].get("files") or [])

    # ---- Phase 2a : walls --------------------------------------------
    if args.verbose:
        print("[dryrun] Phase 2a — extract_wall_thicknesses_many", file=sys.stderr)
    report["extract_wall_thicknesses_many"] = dwg_import.extract_wall_thicknesses_many(
        kg=kg, file_paths=[str(p) for p in plans],
    )

    # Map plan → level for create_continuous_walls_many.
    plan_items: List[Dict[str, Any]] = []
    levels_sorted = sorted(
        kg.find_by_type("Level"),
        key=lambda lid: kg.get_node(lid)["elevation"],
    )
    if not levels_sorted:
        print("[dryrun] No levels created — Phase 2 aborts.", file=sys.stderr)
        report["phase2_skipped"] = "no levels"
    else:
        from lib.tools.dwg_import import _plan_path_to_level_elev
        plan_level_elev = _plan_path_to_level_elev(kg)
        for p in plans:
            elev = plan_level_elev.get(str(p))
            level_ref = None
            for lid in levels_sorted:
                if abs(kg.get_node(lid)["elevation"] - (elev or 0.0)) < 0.01:
                    level_ref = lid
                    break
            if level_ref is None:
                # Fallback to lowest level.
                level_ref = levels_sorted[0]
            plan_items.append({
                "file_path": str(p),
                "level_ref": level_ref,
                "height_m": 3.0,
            })

        if args.verbose:
            print("[dryrun] Phase 2a — create_continuous_walls_many", file=sys.stderr)
        report["create_continuous_walls_many"] = dwg_import.create_continuous_walls_many(
            kg=kg, doc=None, items=plan_items,
        )

        # ---- Phase 2b : openings -------------------------------------
        if args.verbose:
            print("[dryrun] Phase 2b — add_openings_to_walls_many", file=sys.stderr)
        report["add_openings_to_walls_many"] = dwg_import.add_openings_to_walls_many(
            kg=kg, doc=None,
        )

        # ---- Phase 2c : floors ---------------------------------------
        if args.verbose:
            print("[dryrun] Phase 2c — create_floors_many", file=sys.stderr)
        report["create_floors_many"] = dwg_import.create_floors_many(
            kg=kg, doc=None,
        )

    # ---- Final KG snapshot -------------------------------------------
    report["kg_final"] = _dump_kg(kg)

    # Path normalisation : the dryrun output is committed as a golden
    # fixture, so absolute paths (which differ between machines) get
    # replaced by a stable placeholder. The project_dir is mapped to
    # "<P>", any other path is left as-is. Done last so KG nodes are
    # already in the report.
    placeholder = "<P>"
    target = str(project_dir)
    serialised = json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False)
    serialised = serialised.replace(target.replace("\\", "\\\\"), placeholder)
    serialised = serialised.replace(target, placeholder)
    out_path.write_text(serialised, encoding="utf-8")
    if args.verbose:
        print("[dryrun] Wrote {}".format(out_path), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 2
    sys.exit(rc)
