"""tools/views.py — création de vues Revit (Section + linkage CAD).

Phase 1 Étape 6 import projet (cf. JOURNAL 2026-05-13 spec) : permet à
l'agent de poser les références visuelles dans Revit avant la phase 2
d'import du modèle.

**Pas dans le KG en V0**. Les vues Revit n'ont pas de schema node — un
ViewSection nouvellement créé existe seulement côté Revit. Si l'agent
veut tracer le mapping (DXF coupe → ViewSection Revit), il utilise
`dxf_context_register_linked_view` après création.

**Tier-2** : ces tools ne sont chargés que pour les prompts d'import
(« importe ce projet », « crée les coupes Revit », etc.).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..llm_protocol import tool
from ..project_kg import ProjectKG
from ..section_view_geom import compute_section_view_bounds


# ----- views_create_section --------------------------------------------


@tool(name="views_create_section", tier=2)
def create_section(
    kg: ProjectKG,
    doc: Any,
    name: str,
    p1_m: List[float],
    p2_m: List[float],
    view_dir: str,
    bottom_elev_m: float = 0.0,
    top_elev_m: float = 6.0,
    far_clip_m: float = 20.0,
    height_buffer_m: float = 1.0,
) -> Dict[str, Any]:
    """Crée une vue ViewSection Revit le long du trait de coupe spécifié.

    Use case : après que l'agent a identifié les traits de coupe en plan
    (via `dwg_find_section_markers`) et a confirmé le mapping avec
    l'utilisateur, il crée la vue Revit correspondante pour pouvoir y
    ré-importer le DXF coupe en référence.

    Convention de direction : `view_dir` est la direction de regard
    dans le plan. Voir [SectionViewBounds]. Si vertical section line,
    `view_dir` ∈ {left, right} ; si horizontal, ∈ {up, down}.

    **Pas bindé au KG** : la vue Revit est créée mais son revit_id est
    juste retourné. Le caller persiste éventuellement via
    `dxf_context_register_linked_view`.

    Concepts: vue, section, coupe, view section, Revit, plan d'étage,
              import projet, phase 1, géo-ref
    Phrases: "crée une coupe Revit", "ajoute la vue de la coupe 1",
             "section view", "fais apparaître la coupe dans Revit"
    Similar: dwg_find_section_markers, views_link_cad,
             dxf_context_register_section_line

    Args:
        name: nom de la vue (ex `"Coupe A-A"`, `"Coupe 1"`). Unique
            dans le projet — Revit refuse les doublons.
        p1_m: endpoint 1 du trait de coupe en plan, `[x, y]` m.
        p2_m: endpoint 2 du trait de coupe en plan, `[x, y]` m.
        view_dir: direction de regard dans le plan,
            `"left"`/`"right"`/`"up"`/`"down"`.
        bottom_elev_m: bas de la vue (défaut 0 m = niveau sol).
        top_elev_m: haut de la vue (défaut 6 m).
        far_clip_m: profondeur de coupe vers l'arrière (défaut 20 m).
        near_clip_m: clip avant (défaut 1 m).
        height_buffer_m: marge au-dessus de top_elev (défaut 1 m).

    Returns:
        {"ok": bool, "name": str, "revit_id": int | None,
         "section_length_m": float, "view_dir": str}
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")

    bounds = compute_section_view_bounds(
        p1_m, p2_m, view_dir,
        bottom_elev_m=bottom_elev_m, top_elev_m=top_elev_m,
        far_clip_m=far_clip_m, height_buffer_m=height_buffer_m,
    )

    if doc is None:
        return {
            "ok": True,
            "name": name.strip(),
            "revit_id": None,
            "section_length_m": round(bounds.section_length_m, 4),
            "view_dir": view_dir,
            "note": "doc is None — geometry computed but no Revit ViewSection created.",
        }

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import (
        BoundingBoxXYZ, FilteredElementCollector, Transform, ViewFamily,
        ViewFamilyType, ViewSection, XYZ,
    )

    # Find a Section ViewFamilyType.
    vft = None
    for v in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if v.ViewFamily == ViewFamily.Section:
            vft = v
            break
    if vft is None:
        raise ValueError(
            "No Section ViewFamilyType in this project — cannot create "
            "ViewSection. The template may be missing the standard "
            "Section view family."
        )

    # Build Transform.
    t = Transform.Identity
    t.Origin = XYZ(
        rp.meters_to_internal(bounds.origin_m[0]),
        rp.meters_to_internal(bounds.origin_m[1]),
        rp.meters_to_internal(bounds.origin_m[2]),
    )
    t.BasisX = XYZ(*bounds.basis_x)
    t.BasisY = XYZ(*bounds.basis_y)
    t.BasisZ = XYZ(*bounds.basis_z)

    # Build BoundingBoxXYZ in local frame.
    bbox = BoundingBoxXYZ()
    bbox.Transform = t
    bbox.Min = XYZ(
        rp.meters_to_internal(bounds.bbox_min_m[0]),
        rp.meters_to_internal(bounds.bbox_min_m[1]),
        rp.meters_to_internal(bounds.bbox_min_m[2]),
    )
    bbox.Max = XYZ(
        rp.meters_to_internal(bounds.bbox_max_m[0]),
        rp.meters_to_internal(bounds.bbox_max_m[1]),
        rp.meters_to_internal(bounds.bbox_max_m[2]),
    )

    revit_id: Optional[int] = None
    with rp.transaction(doc, "views.create_section"):
        view = ViewSection.CreateSection(doc, vft.Id, bbox)
        # Rename — Revit auto-names new sections like "Section 1".
        view.Name = name.strip()
        revit_id = int(view.Id.Value)

    return {
        "ok": True,
        "name": name.strip(),
        "revit_id": revit_id,
        "section_length_m": round(bounds.section_length_m, 4),
        "view_dir": view_dir,
    }


# ----- views_link_cad --------------------------------------------------


@tool(name="views_link_cad", tier=2)
def link_cad(
    kg: ProjectKG,
    doc: Any,
    file_path: str,
    view_revit_id: Optional[int] = None,
    placement: str = "origin",
    color_mode: str = "preserved",
) -> Dict[str, Any]:
    """Insère un DXF dans une vue Revit en tant que LIEN (pas import dur).

    Use case : poser les DXF plan + coupes en référence visuelle dans
    les vues Revit correspondantes, après création des ViewSection.

    « Lien » signifie qu'on garde la référence vers le fichier source —
    si le DXF est modifié et re-pointé, le lien se met à jour. Diffère
    d'« import » qui copie la géométrie dans le projet Revit (statique).

    Concepts: dxf, dwg, lien, link, import, cad, vue, view, référence,
              dessin, phase 1, géo-ref
    Phrases: "lie ce DXF dans la vue", "import DXF en lien",
             "link CAD reference", "dessin de référence"
    Similar: views_create_section, dwg_inspect_sections,
             dxf_context_register_linked_view

    Args:
        file_path: chemin du DXF à linker (plan ou coupe).
        view_revit_id: revit_id de la vue cible. Si None, lien dans
            la vue active (UIDocument.ActiveView). En pratique l'agent
            passera le revit_id retourné par `views_create_section`
            pour les coupes, et le revit_id du plan d'étage pour le
            plan.
        placement: `"origin"` (point d'insertion = origine du fichier) ou
            `"center"` (centré sur l'écran). Défaut `"origin"`.
        color_mode: `"preserved"`, `"black_and_white"`, ou
            `"by_layer"`. Défaut `"preserved"`.

    Returns:
        {"ok": bool, "file": str, "view_revit_id": int | None,
         "link_revit_id": int | None}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    if placement not in ("origin", "center"):
        raise ValueError("placement must be 'origin' or 'center'")
    if color_mode not in ("preserved", "black_and_white", "by_layer"):
        raise ValueError("color_mode must be 'preserved', 'black_and_white', or 'by_layer'")

    if doc is None:
        return {
            "ok": True,
            "file": str(path),
            "view_revit_id": view_revit_id,
            "link_revit_id": None,
            "note": "doc is None — no Revit link created.",
        }

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import (
        DWGImportOptions, ElementId, ImportColorMode, ImportPlacement,
    )
    import clr

    placement_map = {
        "origin": ImportPlacement.Origin,
        "center": ImportPlacement.Centered,
    }
    # ImportColorMode enum (Revit 2025) : Preserved, BlackAndWhite.
    # `PreserveColorMode` n'existe pas — bug initial reporté runtime
    # 2026-05-13. `by_layer` est synonymé sur Preserved (mapping
    # source layer color → output layer color = preserve).
    color_map = {
        "preserved": ImportColorMode.Preserved,
        "black_and_white": ImportColorMode.BlackAndWhite,
        "by_layer": ImportColorMode.Preserved,
    }

    # Resolve target view.
    if view_revit_id is None:
        # Default to active view.
        uidoc = getattr(doc, "Application", None)
        # We can't reliably get UIDocument from Document — caller must
        # pass view_revit_id explicitly when not using ActiveView pattern.
        raise ValueError(
            "view_revit_id required (no automatic ActiveView fallback "
            "in V0). Pass the revit_id of the target view."
        )

    view = doc.GetElement(ElementId(view_revit_id))
    if view is None:
        raise ValueError(
            "View {} not found in document. Run Refresh KG or check "
            "the id.".format(view_revit_id)
        )

    options = DWGImportOptions()
    options.AutoCorrectAlmostVHLines = False
    options.Placement = placement_map[placement]
    options.ColorMode = color_map[color_mode]
    options.OrientToView = True

    out_id = clr.Reference[ElementId]()

    link_revit_id: Optional[int] = None
    with rp.transaction(doc, "views.link_cad"):
        ok = doc.Link(str(path), options, view, out_id)
        if not ok or out_id.Value is None:
            raise RuntimeError(
                "doc.Link returned False for {}. The file may be "
                "corrupted, the view may not accept CAD links, or "
                "Revit refused for an unspecified reason.".format(path.name)
            )
        link_revit_id = int(out_id.Value.Value)

    return {
        "ok": True,
        "file": str(path),
        "view_revit_id": view_revit_id,
        "link_revit_id": link_revit_id,
        "placement": placement,
        "color_mode": color_mode,
    }
