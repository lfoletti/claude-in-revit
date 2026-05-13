"""tools/dxf_context.py — persistance Phase 1 import DXF (UC1 §JOURNAL 2026-05-13).

`DxfImportContext` est un singleton-ish (max 1 vivant par projet KG)
qui matérialise les décisions prises pendant l'import projet :

- inspection des fichiers (kind, source)
- traits de coupe localisés (auto + user pointage)
- échelle vérifiée plan ↔ coupe
- niveaux reconciliés (créés / modifiés / supprimés)
- liens CAD posés dans Revit

Le KG est l'unique source de vérité — pas de fichier de session
séparé. Si l'agent perd le contexte conversationnel (compaction,
reset), il rappelle `dxf_context_get` pour reconstruire l'état.

**Pourquoi pas de schéma fort par sous-attr** : chaque champ
(section_lines, level_reconciliation, ...) est libre dans `optional`
parce que les schémas internes évoluent vite (Phase B/C/D du chantier
coupes). Validation stricte = friction inutile à ce stade. Si une
clé devient load-bearing, on la promote en sous-node KG.

Tier-1 : ces tools sont appelés sur tous les imports DXF, donc pas
de routing tier-2 nécessaire.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG


_NODE_TYPE = "DxfImportContext"


def _find_live_context(kg: ProjectKG) -> Optional[str]:
    """Renvoie le llm_id du DxfImportContext vivant, ou None s'il n'y en
    a pas. Si plusieurs (anomalie historique), renvoie le plus récent.
    """
    candidates: List[str] = []
    for nid in kg.find_by_type(_NODE_TYPE):
        node = kg.get_node(nid)
        if node.get("deleted_at_turn") is None:
            candidates.append(nid)
    if not candidates:
        return None
    # Most recent = highest counter suffix.
    candidates.sort(key=lambda lid: int(lid.rsplit("_", 1)[1]))
    return candidates[-1]


# ----- dxf_context_get --------------------------------------------------


@tool(name="dxf_context_get", tier=1)
def get_context(kg: ProjectKG) -> Dict[str, Any]:
    """Lit le `DxfImportContext` vivant du projet (s'il existe).

    Permet à l'agent de retrouver les décisions Phase 1 prises au tour
    précédent (fichiers identifiés, traits de coupe pointés par l'user,
    niveaux reconciliés, etc.) sans tout redemander.

    Concepts: dxf, import, projet, contexte, état, phase 1, reprise,
              section_lines, niveaux, plan d'étage
    Phrases: "où en est-on dans l'import du projet",
             "résume l'état de l'import DXF",
             "rappelle-moi les traits de coupe", "context import"
    Similar: dxf_context_register_inspection, dxf_context_register_section_line,
             dwg_inspect_sections

    Args:
        (aucun)

    Returns:
        {"exists": bool, "llm_id": str | None, "directory": str | None,
         "source": str | None, "files": list, "section_lines": list,
         "level_reconciliation": dict | None, "linked_views": list}
    """
    nid = _find_live_context(kg)
    if nid is None:
        return {
            "exists": False,
            "llm_id": None,
            "directory": None,
            "source": None,
            "files": [],
            "section_lines": [],
            "level_reconciliation": None,
            "linked_views": [],
        }
    node = kg.get_node(nid)
    return {
        "exists": True,
        "llm_id": nid,
        "directory": node.get("directory"),
        "source": node.get("source"),
        "files": list(node.get("files", [])),
        "section_lines": list(node.get("section_lines", [])),
        "level_reconciliation": node.get("level_reconciliation"),
        "linked_views": list(node.get("linked_views", [])),
    }


# ----- dxf_context_register_inspection ---------------------------------


@tool(name="dxf_context_register_inspection", tier=1)
def register_inspection(
    kg: ProjectKG,
    directory: str,
    inspection: Dict[str, Any],
    source: Optional[str] = None,
) -> Dict[str, Any]:
    """Matérialise le résultat de `dwg_inspect_sections` dans le KG.

    Crée le `DxfImportContext` s'il n'existe pas, ou met à jour le
    champ `files` avec la nouvelle inspection (kind, path) sinon.

    **Idempotent** : appeler 2× avec la même inspection ne crée pas
    de doublon — le node existant est modifié.

    Concepts: dxf, import, contexte, inspection, persistance, kg, projet
    Phrases: "enregistre l'inspection", "sauvegarde l'état projet",
             "persiste l'import DXF", "context register"
    Similar: dwg_inspect_sections, dxf_context_get,
             dxf_context_register_section_line

    Args:
        directory: chemin du dossier source des DXF (clé de l'import).
        inspection: dict retourné par `dwg_inspect_sections`. Le tool
            ne lit que `inspection["files"]` (path + kind).
        source: identifiant de la source d'export DXF, ex `"revit_aia"`,
            `"archicad"`, `"vectorworks"`, `"other"`. Optionnel — peut
            être posé plus tard via une étape source-detection dédiée.

    Returns:
        {"ok": bool, "llm_id": str, "created": bool,
         "files_count": int, "directory": str}
    """
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("directory must be a non-empty string")
    if not isinstance(inspection, dict):
        raise ValueError("inspection must be a dict (output of dwg_inspect_sections)")
    files_raw = inspection.get("files", [])
    if not isinstance(files_raw, list):
        raise ValueError("inspection['files'] must be a list")

    files_compact: List[Dict[str, Any]] = []
    for f in files_raw:
        if not isinstance(f, dict):
            continue
        files_compact.append({
            "path": f.get("path"),
            "name": f.get("name"),
            "kind": f.get("kind"),
        })

    nid = _find_live_context(kg)
    if nid is None:
        attrs: Dict[str, Any] = {
            "directory": directory,
            "files": files_compact,
            "section_lines": [],
            "linked_views": [],
        }
        if source is not None:
            attrs["source"] = source
        nid = kg.add_node(_NODE_TYPE, attrs)
        created = True
    else:
        updates: Dict[str, Any] = {
            "directory": directory,
            "files": files_compact,
        }
        if source is not None:
            updates["source"] = source
        kg.modify_node(nid, updates)
        created = False

    return {
        "ok": True,
        "llm_id": nid,
        "created": created,
        "files_count": len(files_compact),
        "directory": directory,
    }


# ----- dxf_context_register_section_line --------------------------------


@tool(name="dxf_context_register_section_line", tier=1)
def register_section_line(
    kg: ProjectKG,
    coupe_path: str,
    plan_p1: List[float],
    plan_p2: List[float],
    view_dir: str,
    name: Optional[str] = None,
    confirmed_by_user: bool = False,
    scale_verified: bool = False,
    drift_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """Enregistre dans le KG le trait de coupe qui mappe une coupe DXF
    à un axe dans le plan.

    Use case : l'agent vient d'identifier (auto ou via user) où passe la
    Coupe 1 dans le plan (segment p1→p2 + direction de vue). Il
    persiste cette info pour pouvoir mapper plus tard les ouvertures
    coupe ↔ plan.

    **Append-only** : appel répété ajoute une entry. Pour remplacer, il
    faut explicitement effacer (pas de tool dédié — réservé à des cas
    rares, l'agent peut passer par `dxf_context_clear_section_lines`
    si on l'ajoute plus tard).

    Concepts: dxf, coupe, section line, trait de coupe, géo-ref, plan,
              contexte, persistance, pointage
    Phrases: "enregistre le trait de coupe", "la coupe 1 passe par
             ces points", "store section line"
    Similar: dxf_context_get, dwg_find_section_markers,
             dwg_verify_section_scale

    Args:
        coupe_path: chemin du DXF coupe que ce trait référence.
        plan_p1: [x, y] en mètres dans le plan, début du trait.
        plan_p2: [x, y] en mètres dans le plan, fin du trait.
        view_dir: direction de vue depuis le trait. Valeurs canoniques :
            `"left"`, `"right"` (perpendiculaire au trait, côté
            relatif au sens p1→p2). Aussi `"up"`, `"down"` accepté
            quand le trait est horizontal/vertical, plus parlant.
        name: étiquette de la coupe (ex `"A-A"`, `"Coupe 1"`). Optionnel.
        confirmed_by_user: True si l'user a explicitement validé ces
            coords (vs auto-détecté). Façonne la confiance affichée.
        scale_verified: True si l'étape 3 a confirmé que ||p2-p1|| ≈
            extension X de la coupe. Posé plus tard par
            `dwg_verify_section_scale`.
        drift_pct: drift d'échelle observé en %, posé si scale_verified
            est True.

    Returns:
        {"ok": bool, "context_llm_id": str,
         "section_line_index": int, "total_section_lines": int}
    """
    if not isinstance(coupe_path, str) or not coupe_path.strip():
        raise ValueError("coupe_path required (str)")
    if not (isinstance(plan_p1, list) and len(plan_p1) == 2):
        raise ValueError("plan_p1 must be [x, y] in m")
    if not (isinstance(plan_p2, list) and len(plan_p2) == 2):
        raise ValueError("plan_p2 must be [x, y] in m")
    if view_dir not in ("left", "right", "up", "down"):
        raise ValueError(
            "view_dir must be one of: left, right, up, down (got {!r})".format(view_dir)
        )

    nid = _find_live_context(kg)
    if nid is None:
        # Auto-create un context minimal (sans directory connu). L'agent
        # devrait normalement avoir appelé register_inspection avant, mais
        # ne pas bloquer une session où il enregistre les section_lines
        # directement après pointage user.
        nid = kg.add_node(_NODE_TYPE, {
            "directory": "",
            "files": [],
            "section_lines": [],
            "linked_views": [],
        })

    node = kg.get_node(nid)
    section_lines = list(node.get("section_lines", []))
    entry: Dict[str, Any] = {
        "name": name,
        "coupe_path": coupe_path,
        "plan_p1": [float(plan_p1[0]), float(plan_p1[1])],
        "plan_p2": [float(plan_p2[0]), float(plan_p2[1])],
        "view_dir": view_dir,
        "confirmed_by_user": bool(confirmed_by_user),
        "scale_verified": bool(scale_verified),
    }
    if drift_pct is not None:
        entry["drift_pct"] = float(drift_pct)
    section_lines.append(entry)
    kg.modify_node(nid, {"section_lines": section_lines})

    return {
        "ok": True,
        "context_llm_id": nid,
        "section_line_index": len(section_lines) - 1,
        "total_section_lines": len(section_lines),
    }


# ----- dxf_context_register_linked_view --------------------------------


@tool(name="dxf_context_register_linked_view", tier=1)
def register_linked_view(
    kg: ProjectKG,
    file_path: str,
    link_revit_id: int,
    view_revit_id: int,
    view_kind: str,
    view_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Enregistre dans le KG un DXF qui a été linké dans une vue Revit.

    Use case : après `views_link_cad` réussi, l'agent persiste le
    mapping (file → link_revit_id, view_revit_id) pour pouvoir retrouver
    le lien plus tard (purge, mise à jour, etc.).

    **Append-only** : appels répétés ajoutent une entry. Permet de
    linker le même DXF dans plusieurs vues sans collision.

    Concepts: dxf, link, lien, view, vue, cad, persistance, kg, contexte
    Phrases: "enregistre le lien", "save the link", "store linked view"
    Similar: views_link_cad, dxf_context_get,
             dxf_context_register_section_line

    Args:
        file_path: chemin du DXF linké.
        link_revit_id: revit_id de l'ImportInstance (retourné par
            views_link_cad).
        view_revit_id: revit_id de la vue cible.
        view_kind: `"plan"` ou `"section"` (pour aider l'agent à
            naviguer plus tard).
        view_name: nom de la vue (facultatif, redondant avec view_revit_id
            mais utile pour la lisibilité agent).

    Returns:
        {"ok": bool, "context_llm_id": str, "linked_view_index": int,
         "total_linked_views": int}
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("file_path required (str)")
    if not isinstance(link_revit_id, int) or link_revit_id <= 0:
        raise ValueError("link_revit_id required (positive int)")
    if not isinstance(view_revit_id, int) or view_revit_id <= 0:
        raise ValueError("view_revit_id required (positive int)")
    if view_kind not in ("plan", "section"):
        raise ValueError(
            "view_kind must be 'plan' or 'section' (got {!r})".format(view_kind)
        )

    nid = _find_live_context(kg)
    if nid is None:
        nid = kg.add_node(_NODE_TYPE, {
            "directory": "",
            "files": [],
            "section_lines": [],
            "linked_views": [],
        })

    node = kg.get_node(nid)
    linked = list(node.get("linked_views", []))
    entry = {
        "file_path": file_path,
        "link_revit_id": link_revit_id,
        "view_revit_id": view_revit_id,
        "view_kind": view_kind,
    }
    if view_name is not None:
        entry["view_name"] = view_name
    linked.append(entry)
    kg.modify_node(nid, {"linked_views": linked})

    return {
        "ok": True,
        "context_llm_id": nid,
        "linked_view_index": len(linked) - 1,
        "total_linked_views": len(linked),
    }
