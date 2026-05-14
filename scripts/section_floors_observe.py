"""scripts/section_floors_observe.py — observation hors-Revit pour calibrer
la cross-validation des dalles plan↔coupe.

Pour chaque DXF de coupe dans le dossier passé en argument :

  1. Liste tous les layers et le nombre de LINEs horizontales par layer
     (pour repérer où vivent les bandeaux de dalle au-delà de A-FLOR).
  2. Énumère toutes les LINEs horizontales sur ``A-FLOR`` avec
     (y, x_min, x_max, longueur).
  3. Lance ``read_section_floor_slabs()`` et liste les paires retenues.
  4. Croise (2) et (3) pour identifier les LINEs A-FLOR horizontales
     **orphelines** (pas appariées) — c'est le signal pour savoir si le
     pairing est trop strict ou si la donnée est bruitée.
  5. Lance ``read_levels()`` et reporte les Z des niveaux extraits, pour
     comparer aux Y des paires détectées.

Et pour chaque DXF de plan dans le même dossier :

  6. Compte les LWPOLYLINEs fermées et LINEs sur A-FLOR, et les
     polylignes fermées sur layers de trou (A-FLOR-STAIR, etc.) — pour
     savoir d'où viennent les dalles plan-side et donc ce qui doit être
     cross-validé.

Usage::

    python -m scripts.section_floors_observe --project-dir tests/fixtures/P7
    python -m scripts.section_floors_observe --project-dir <path> --out report.json

Exit codes: 0 success, 1 unexpected error (path, etc.), 2 parse error.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# sys.path fixup — identique à dxf_dryrun.py.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB_HOST = _REPO_ROOT / "claude-in-revit.extension"
if str(_LIB_HOST) not in sys.path:
    sys.path.insert(0, str(_LIB_HOST))


_HORIZONTAL_TOL_M = 0.005  # même seuil que read_section_floor_slabs.


def _classify_dxfs(project_dir: Path) -> Tuple[List[Path], List[Path]]:
    """Retourne (plans, coupes) par heuristique nom de fichier — calé
    sur la convention export Revit utilisée dans tests/fixtures/P7.
    """
    plans: List[Path] = []
    coupes: List[Path] = []
    for p in sorted(project_dir.glob("*.dxf")):
        name = p.name.lower()
        if "plan d'étage" in name or "plan d'etage" in name:
            plans.append(p)
        elif "coupe" in name:
            coupes.append(p)
    return plans, coupes


def _is_horizontal(p1, p2) -> bool:
    return abs(p2[1] - p1[1]) <= _HORIZONTAL_TOL_M


def _horizontal_lines_per_layer(entities) -> Dict[str, int]:
    """Compte les LINEs horizontales par layer."""
    counts: Dict[str, int] = defaultdict(int)
    for e in entities:
        if e.kind != "LINE":
            continue
        p1, p2 = e.coords
        if _is_horizontal(p1, p2):
            counts[e.layer] += 1
    return dict(counts)


def _a_flor_horizontal_lines(entities) -> List[Dict[str, float]]:
    """Liste toutes les LINEs horizontales sur le layer exact ``A-FLOR``
    (le seul layer accepté par ``read_section_floor_slabs``).
    """
    from lib.dwg_section_reader import LAYER_FLOORS
    out: List[Dict[str, float]] = []
    for e in entities:
        if e.layer != LAYER_FLOORS or e.kind != "LINE":
            continue
        (x1, y1, _), (x2, y2, _) = e.coords
        if abs(y2 - y1) > _HORIZONTAL_TOL_M:
            continue
        y = round((y1 + y2) / 2.0, 4)
        x_min, x_max = (x1, x2) if x1 <= x2 else (x2, x1)
        out.append({
            "y_m": y,
            "x_min_m": round(x_min, 4),
            "x_max_m": round(x_max, 4),
            "length_m": round(x_max - x_min, 4),
        })
    out.sort(key=lambda r: (r["y_m"], r["x_min_m"]))
    return out


def _match_orphans(
    horiz: List[Dict[str, float]],
    slabs,
) -> List[Dict[str, float]]:
    """Une LINE A-FLOR horizontale est *consommée* par un slab si son y
    coïncide (à 1mm) avec top_y_m ou bot_y_m du slab ET son x_range
    intersecte celui du slab. Retourne celles qui restent orphelines.
    """
    orphans: List[Dict[str, float]] = []
    for h in horiz:
        consumed = False
        for s in slabs:
            y_match = (
                abs(h["y_m"] - s.top_y_m) < 0.002
                or abs(h["y_m"] - s.bot_y_m) < 0.002
            )
            if not y_match:
                continue
            # overlap X ?
            ox = max(h["x_min_m"], s.x_min_m)
            oX = min(h["x_max_m"], s.x_max_m)
            if oX > ox:
                consumed = True
                break
        if not consumed:
            orphans.append(h)
    return orphans


def _observe_section(p: Path, scale_override: Optional[float] = None) -> Dict[str, Any]:
    """Bloc d'observation pour une coupe."""
    from lib import dwg_reader, dwg_section_reader
    ents, meta = dwg_reader.parse(p, scale_override=scale_override)

    levels = dwg_section_reader.read_levels(ents)
    slabs = dwg_section_reader.read_section_floor_slabs(ents)
    horiz_a_flor = _a_flor_horizontal_lines(ents)
    horiz_per_layer = _horizontal_lines_per_layer(ents)
    orphans = _match_orphans(horiz_a_flor, slabs)

    return {
        "file": p.name,
        "insunits_meters_factor": meta.get("unit_factor"),
        "entity_count": len(ents),
        "levels_found": [
            {"name": lv.name, "z_m": lv.elevation_m} for lv in levels
        ],
        "slabs_paired": [
            {
                "top_y_m": round(s.top_y_m, 4),
                "bot_y_m": round(s.bot_y_m, 4),
                "thickness_m": round(s.thickness_m, 4),
                "x_min_m": round(s.x_min_m, 4),
                "x_max_m": round(s.x_max_m, 4),
                "x_length_m": round(s.x_max_m - s.x_min_m, 4),
            }
            for s in slabs
        ],
        "horizontal_lines_per_layer": dict(sorted(
            horiz_per_layer.items(), key=lambda kv: -kv[1]
        )),
        "a_flor_horizontal_lines_total": len(horiz_a_flor),
        "a_flor_horizontal_lines_consumed_by_pairs": len(horiz_a_flor) - len(orphans),
        "a_flor_horizontal_orphans": orphans,
    }


def _observe_plan(p: Path, scale_override: Optional[float] = None) -> Dict[str, Any]:
    """Bloc d'observation pour un plan : combien d'A-FLOR LINEs/closed
    polylines (priorité 1 du floor extraction), combien de polylignes
    fermées sur layers de trou.
    """
    from lib import dwg_reader, dwg_section_reader
    ents, meta = dwg_reader.parse(p, scale_override=scale_override)

    a_flor_lines = 0
    a_flor_closed_polylines = 0
    a_flor_open_polylines = 0
    for e in ents:
        if e.layer != "A-FLOR":
            continue
        if e.kind == "LINE":
            a_flor_lines += 1
        elif e.kind in ("LWPOLYLINE", "POLYLINE"):
            if e.attrs.get("closed"):
                a_flor_closed_polylines += 1
            else:
                a_flor_open_polylines += 1

    holes = dwg_section_reader.read_floor_holes_from_plan(ents)
    holes_by_kind: Dict[str, int] = defaultdict(int)
    for h in holes:
        holes_by_kind[h.kind] += 1

    return {
        "file": p.name,
        "entity_count": len(ents),
        "a_flor_LINE_count": a_flor_lines,
        "a_flor_LWPOLYLINE_closed_count": a_flor_closed_polylines,
        "a_flor_LWPOLYLINE_open_count": a_flor_open_polylines,
        "floor_holes_by_kind": dict(holes_by_kind),
        "floor_holes_total": len(holes),
    }


def _format_text_report(report: Dict[str, Any]) -> str:
    """Rapport humain — c'est ce qu'on regarde pour calibrer."""
    lines: List[str] = []
    lines.append(f"# Observation section_floors — {report['project_dir']}")
    lines.append("")

    lines.append(f"## Plans ({len(report['plans'])})")
    for pl in report["plans"]:
        lines.append(f"### {pl['file']}")
        lines.append(f"  entités       : {pl['entity_count']}")
        lines.append(f"  A-FLOR LINE   : {pl['a_flor_LINE_count']}")
        lines.append(f"  A-FLOR LWPOLYLINE closed : {pl['a_flor_LWPOLYLINE_closed_count']}")
        lines.append(f"  A-FLOR LWPOLYLINE open   : {pl['a_flor_LWPOLYLINE_open_count']}")
        lines.append(f"  trous de dalle (par kind): {pl['floor_holes_by_kind']}  total={pl['floor_holes_total']}")
        lines.append("")

    lines.append(f"## Coupes ({len(report['coupes'])})")
    for co in report["coupes"]:
        lines.append(f"### {co['file']}")
        lines.append(f"  entités          : {co['entity_count']}")
        lines.append(f"  unit_factor (m)  : {co['insunits_meters_factor']}")
        lines.append(f"  niveaux extraits : "
                     + ", ".join(f"{lv['name']}@{lv['z_m']:.3f}" for lv in co["levels_found"])
                     or "  niveaux extraits : (aucun)")
        lines.append("")
        lines.append("  Layers triés par count de LINEs horizontales :")
        for layer, n in co["horizontal_lines_per_layer"].items():
            lines.append(f"    {n:5d}  {layer}")
        lines.append("")
        nh = co["a_flor_horizontal_lines_total"]
        nc = co["a_flor_horizontal_lines_consumed_by_pairs"]
        no = len(co["a_flor_horizontal_orphans"])
        lines.append(f"  A-FLOR horizontales : {nh} total ({nc} appariées, {no} orphelines)")
        lines.append("")
        lines.append(f"  Paires (top, bot, thk, x_min..x_max [longueur]) — {len(co['slabs_paired'])}")
        for s in co["slabs_paired"]:
            lines.append(
                f"    top={s['top_y_m']:.3f}  bot={s['bot_y_m']:.3f}  "
                f"thk={s['thickness_m']*100:.1f}cm  "
                f"x=[{s['x_min_m']:.2f}, {s['x_max_m']:.2f}] L={s['x_length_m']:.2f}m"
            )
        if co["a_flor_horizontal_orphans"]:
            lines.append("")
            lines.append("  Orphelines A-FLOR (y, x_min..x_max, longueur) :")
            for h in co["a_flor_horizontal_orphans"]:
                lines.append(
                    f"    y={h['y_m']:.3f}  "
                    f"x=[{h['x_min_m']:.2f}, {h['x_max_m']:.2f}]  L={h['length_m']:.2f}m"
                )
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-dir", default="tests/fixtures/P7",
        help="Dossier contenant les DXFs plan + coupe.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Chemin JSON de sortie. Par défaut: <project_dir>/section_floors_observe.json",
    )
    parser.add_argument(
        "--scale-override", type=float, default=None,
        help="Facteur unit override si le DXF est unitless.",
    )
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"Not a directory: {project_dir}", file=sys.stderr)
        return 1
    out_path = (
        Path(args.out) if args.out
        else project_dir / "section_floors_observe.json"
    )

    plans, coupes = _classify_dxfs(project_dir)
    if not coupes:
        print(f"No coupe DXFs in {project_dir}", file=sys.stderr)
        return 1

    report: Dict[str, Any] = {
        "project_dir": str(project_dir),
        "plans": [],
        "coupes": [],
    }
    for p in plans:
        report["plans"].append(_observe_plan(p, scale_override=args.scale_override))
    for c in coupes:
        report["coupes"].append(_observe_section(c, scale_override=args.scale_override))

    # Sauvegarde JSON pour suite (cross-validation contre KG plus tard).
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Rapport humain sur stdout.
    print(_format_text_report(report))
    print(f"\n[obs] JSON écrit dans {out_path}", file=sys.stderr)
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
