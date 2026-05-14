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


@tool(name="views_create_section_many", tier=2)
def create_section_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    bottom_elev_m: float = 0.0,
    top_elev_m: float = 6.0,
    far_clip_m: float = 20.0,
    height_buffer_m: float = 1.0,
) -> Dict[str, Any]:
    """Crée N ViewSections en **une seule** transaction Revit.

    Pattern bulk : évite N round-trips agent ↔ tool. Use case import
    projet : 2-4 coupes à créer simultanément.

    Chaque item : `{name, p1_m, p2_m, view_dir}`. Les autres paramètres
    (elev, clip, buffer) sont communs aux N ViewSections (le default
    s'applique à toutes les coupes du projet en général).

    Transactionnel : si une création échoue, **aucune** n'est commitée.

    Concepts: vue, section, coupe, bulk, batch, plusieurs, phase 1
    Phrases: "crée toutes les coupes", "batch create sections"
    Similar: views_create_section, dwg_find_section_markers

    Args:
        items: liste de specs `{name, p1_m, p2_m, view_dir}`.
        bottom_elev_m, top_elev_m, far_clip_m, height_buffer_m: communs.

    Returns:
        {"ok": bool, "count": int, "sections": [{name, revit_id,
            section_length_m, view_dir}, ...]}
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")

    # Pre-validate.
    specs: List[Dict[str, Any]] = []
    seen_names: set = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                "items[{}] must be a dict".format(i)
            )
        name = item.get("name")
        p1 = item.get("p1_m")
        p2 = item.get("p2_m")
        view_dir = item.get("view_dir")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("items[{}]: name required (str)".format(i))
        if name in seen_names:
            raise ValueError(
                "items[{}]: duplicate name {!r} within batch".format(i, name)
            )
        seen_names.add(name)
        if not (isinstance(p1, list) and len(p1) >= 2):
            raise ValueError("items[{}]: p1_m must be [x, y] in m".format(i))
        if not (isinstance(p2, list) and len(p2) >= 2):
            raise ValueError("items[{}]: p2_m must be [x, y] in m".format(i))
        if view_dir not in ("left", "right", "up", "down"):
            raise ValueError(
                "items[{}]: view_dir must be left/right/up/down".format(i)
            )
        x_axis_conv = item.get("x_axis_convention")  # None par défaut
        if x_axis_conv is not None and x_axis_conv not in ("identity", "reversed"):
            raise ValueError(
                "items[{}]: x_axis_convention must be identity|reversed|None "
                "(got {!r})".format(i, x_axis_conv)
            )
        specs.append({
            "name": name.strip(),
            "p1_m": p1, "p2_m": p2, "view_dir": view_dir,
            "x_axis_convention": x_axis_conv,
        })

    # KG-only : compute bounds + return placeholders.
    if doc is None:
        out: List[Dict[str, Any]] = []
        for spec in specs:
            bounds = compute_section_view_bounds(
                spec["p1_m"], spec["p2_m"], spec["view_dir"],
                bottom_elev_m=bottom_elev_m, top_elev_m=top_elev_m,
                far_clip_m=far_clip_m, height_buffer_m=height_buffer_m,
                x_axis_convention=spec["x_axis_convention"],
            )
            out.append({
                "name": spec["name"],
                "revit_id": None,
                "section_length_m": round(bounds.section_length_m, 4),
                "view_dir": spec["view_dir"],
                "reused": False,
            })
        return {
            "ok": True, "count": len(out),
            "created_count": len(out), "reused_count": 0,
            "sections": out,
            "note": "doc is None — geometry computed but no Revit views created.",
        }

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import (
        BoundingBoxXYZ, FilteredElementCollector, Transform, ViewFamily,
        ViewFamilyType, ViewSection, XYZ,
    )

    # Find section ViewFamilyType once.
    vft = None
    for v in FilteredElementCollector(doc).OfClass(ViewFamilyType):
        if v.ViewFamily == ViewFamily.Section:
            vft = v
            break
    if vft is None:
        raise ValueError(
            "No Section ViewFamilyType in this project."
        )

    # Idempotence : si une ViewSection avec le nom cible existe déjà
    # (typiquement d'un run précédent d'import), la réutiliser au lieu
    # de lever `ArgumentException: Name must be unique`. Géométrie de
    # la vue existante préservée (on ne réécrit pas la bbox) — si l'user
    # a modifié le trait depuis le run précédent, il doit supprimer
    # manuellement la vue pour forcer la régénération.
    existing_by_name: Dict[str, Any] = {}
    for v in FilteredElementCollector(doc).OfClass(ViewSection):
        try:
            existing_by_name[v.Name] = v
        except Exception:  # noqa: BLE001
            continue

    out: List[Dict[str, Any]] = []
    with rp.transaction(doc, "views.create_section_many"):
        for spec in specs:
            bounds = compute_section_view_bounds(
                spec["p1_m"], spec["p2_m"], spec["view_dir"],
                bottom_elev_m=bottom_elev_m, top_elev_m=top_elev_m,
                far_clip_m=far_clip_m, height_buffer_m=height_buffer_m,
                x_axis_convention=spec["x_axis_convention"],
            )
            reused = spec["name"] in existing_by_name
            if reused:
                view = existing_by_name[spec["name"]]
            else:
                t = Transform.Identity
                t.Origin = XYZ(
                    rp.meters_to_internal(bounds.origin_m[0]),
                    rp.meters_to_internal(bounds.origin_m[1]),
                    rp.meters_to_internal(bounds.origin_m[2]),
                )
                t.BasisX = XYZ(*bounds.basis_x)
                t.BasisY = XYZ(*bounds.basis_y)
                t.BasisZ = XYZ(*bounds.basis_z)
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
                view = ViewSection.CreateSection(doc, vft.Id, bbox)
                view.Name = spec["name"]
            # Diagnostic mirror : compare basis_x INTENDED vs ce que
            # Revit a vraiment posé (view.RightDirection). Si Revit
            # re-dérive le basis_x (ignore notre bbox.Transform.BasisX),
            # le flip n'a aucun effet et on doit chercher une autre voie.
            intended_basis_x = bounds.basis_x
            actual_right = None
            try:
                rd = view.RightDirection
                actual_right = [round(rd.X, 4), round(rd.Y, 4), round(rd.Z, 4)]
            except Exception:  # noqa: BLE001
                pass
            out.append({
                "name": spec["name"],
                "revit_id": int(view.Id.Value),
                "section_length_m": round(bounds.section_length_m, 4),
                "view_dir": spec["view_dir"],
                "reused": reused,
                "intended_basis_x": [round(c, 4) for c in intended_basis_x],
                "actual_right_direction": actual_right,
                "basis_x_match": (
                    actual_right is not None
                    and abs(actual_right[0] - intended_basis_x[0]) < 0.01
                    and abs(actual_right[1] - intended_basis_x[1]) < 0.01
                    and abs(actual_right[2] - intended_basis_x[2]) < 0.01
                ),
            })

    reused_count = sum(1 for o in out if o["reused"])
    return {
        "ok": True,
        "count": len(out),
        "created_count": len(out) - reused_count,
        "reused_count": reused_count,
        "sections": out,
    }


@tool(name="views_open_3d", tier=1)
def open_3d(kg: ProjectKG, doc: Any) -> Dict[str, Any]:
    """Active la vue 3D par défaut du projet (pour vérification visuelle).

    Use case canonique : fin de Phase 1 import projet. Après avoir
    créé les sections + linké les DXF, l'agent active la vue 3D pour
    que l'user constate immédiatement que tout est positionné
    correctement avant Phase 2.

    Stratégie :
    1. Cherche un `View3D` nommé `"{3D}"`, `"3D View"`, `"Vue 3D"` (les
       defaults Revit selon la langue du template).
    2. Si pas trouvé : prend le premier View3D non-template, non-perspective.
    3. Active via `UIDocument.ActiveView = view` (PythonNet pyrevit).

    Concepts: vue, view, 3d, default, activate, verification, visuel,
              phase 1
    Phrases: "ouvre la vue 3D", "passe en 3D", "switch to 3D view",
             "montre la vue 3D"
    Similar: views_create_section, levels_create_floor_plan

    Args:
        (aucun)

    Returns:
        {"ok": bool, "activated": bool, "view_revit_id": int | None,
         "view_name": str | None, "note": str | None}
    """
    if doc is None:
        return {
            "ok": True,
            "activated": False,
            "view_revit_id": None,
            "view_name": None,
            "note": "doc is None — pas de Revit, vue 3D non activée.",
        }

    from Autodesk.Revit.DB import FilteredElementCollector, View3D

    default_names = {"{3D}", "{3d}", "3D View", "Vue 3D", "Vue3D"}
    target = None
    candidates: List[Any] = []
    for v in FilteredElementCollector(doc).OfClass(View3D):
        if v.IsTemplate or v.IsPerspective:
            continue
        candidates.append(v)
        if v.Name in default_names:
            target = v
            break
    if target is None and candidates:
        target = candidates[0]

    if target is None:
        return {
            "ok": False,
            "activated": False,
            "view_revit_id": None,
            "view_name": None,
            "note": (
                "Aucune vue 3D disponible dans le projet. Le template "
                "n'en a pas créé automatiquement. User peut en créer "
                "une via Revit UI (Vue → 3D → Default 3D View)."
            ),
        }

    # Activate via UIDocument. uidoc accessible via pyrevit HOST_APP.
    try:
        from pyrevit import HOST_APP
        uidoc = HOST_APP.uidoc
        uidoc.ActiveView = target
    except Exception as e:  # noqa: BLE001
        # Si HOST_APP.uidoc indispo (cas exotique), on échoue mais on
        # retourne tout de même le revit_id pour que l'user puisse
        # activer manuellement.
        return {
            "ok": True,
            "activated": False,
            "view_revit_id": int(target.Id.Value),
            "view_name": target.Name,
            "note": (
                "Vue 3D trouvée ({}) mais activation a échoué : {}. "
                "L'user peut l'ouvrir manuellement via Project Browser."
                .format(target.Name, str(e))
            ),
        }

    return {
        "ok": True,
        "activated": True,
        "view_revit_id": int(target.Id.Value),
        "view_name": target.Name,
    }


def _link_cad_to_view(
    doc: Any,
    path: Path,
    view_revit_id: int,
    placement: str,
    color_mode: str,
    restore_pinned: bool,
    mirror_post_link: bool = False,
) -> Dict[str, Any]:
    """Helper interne : exécute le link + translation + re-pin pour UN
    DXF dans UNE vue. À appeler **dans une transaction Revit ouverte**.

    Factorisé de `views_link_cad` pour permettre le bulk `_many` qui
    enveloppe N items dans une seule transaction (1 Revit Tx vs N).
    """
    from .. import revit_primitives as rp  # noqa: F401  (kept for compatibility, unused here)
    from Autodesk.Revit.DB import (
        DWGImportOptions, ElementId, ElementTransformUtils,
        ImportColorMode, ImportPlacement, ViewSection, XYZ,
    )

    placement_map = {
        "origin": ImportPlacement.Origin,
        "center": ImportPlacement.Centered,
    }
    color_map = {
        "preserved": ImportColorMode.Preserved,
        "black_and_white": ImportColorMode.BlackAndWhite,
        "by_layer": ImportColorMode.Preserved,
    }

    view = doc.GetElement(ElementId(view_revit_id))
    if view is None:
        raise ValueError(
            "View {} not found in document.".format(view_revit_id)
        )

    options = DWGImportOptions()
    options.AutoCorrectAlmostVHLines = False
    options.Placement = placement_map[placement]
    options.ColorMode = color_map[color_mode]
    options.OrientToView = True

    result = doc.Link(str(path), options, view)
    if isinstance(result, tuple):
        ok, out_id = result[0], result[1] if len(result) > 1 else None
    else:
        ok, out_id = bool(result), None
    if not ok:
        raise RuntimeError(
            "doc.Link returned False for {}.".format(path.name)
        )
    link_revit_id: Optional[int] = None
    if out_id is not None:
        try:
            link_revit_id = int(out_id.Value)
        except AttributeError:
            link_revit_id = int(out_id)

    aligned_to_view_origin = False
    final_pinned = False
    if (
        placement == "origin"
        and isinstance(view, ViewSection)
        and out_id is not None
    ):
        view_origin = view.Origin
        basis_x = view.RightDirection
        if abs(basis_x.X) > 0.5:
            origin = XYZ(0.0, view_origin.Y, 0.0)
        else:
            origin = XYZ(view_origin.X, 0.0, 0.0)
        if (
            abs(origin.X) > 1e-9
            or abs(origin.Y) > 1e-9
            or abs(origin.Z) > 1e-9
        ):
            target_eid = (
                out_id if isinstance(out_id, ElementId)
                else ElementId(int(link_revit_id))
            )
            instance = doc.GetElement(target_eid)
            try:
                if instance is not None and getattr(instance, "Pinned", False):
                    instance.Pinned = False
                ElementTransformUtils.MoveElement(doc, target_eid, origin)
                aligned_to_view_origin = True
            except Exception:  # noqa: BLE001
                aligned_to_view_origin = False
            if restore_pinned and instance is not None:
                try:
                    instance.Pinned = True
                    final_pinned = True
                except Exception:  # noqa: BLE001
                    final_pinned = False
    if not final_pinned and link_revit_id is not None:
        try:
            inst = doc.GetElement(ElementId(link_revit_id))
            final_pinned = bool(getattr(inst, "Pinned", False)) if inst else False
        except Exception:  # noqa: BLE001
            final_pinned = False

    # Post-link MIRROR si demandé (fix bug mirror P2 longitudinales) :
    # Revit ignore notre bbox.Transform.BasisX dans CreateSection et
    # re-dérive son propre BasisX (= viewer's right convention). Du
    # coup pour une coupe dont DXF X axis ≠ viewer's right, le DXF est
    # placé miroité dans le monde. Solution : mirror le link element
    # après placement, sur le plan perpendiculaire à world X (pour
    # traits horizontaux) ou world Y (pour traits verticaux).
    mirrored = False
    if (mirror_post_link and link_revit_id is not None
            and isinstance(view, ViewSection)):
        try:
            from Autodesk.Revit.DB import Plane
            # Détermine l'axe de mirror via view.RightDirection :
            # |X| > 0.5 → trait horizontal → mirror plane YZ (normal X).
            # sinon → trait vertical → mirror plane XZ (normal Y).
            right = view.RightDirection
            if abs(right.X) > 0.5:
                normal = XYZ(1.0, 0.0, 0.0)
            else:
                normal = XYZ(0.0, 1.0, 0.0)
            plane = Plane.CreateByNormalAndOrigin(normal, XYZ(0.0, 0.0, 0.0))
            target_eid_for_mirror = (
                out_id if isinstance(out_id, ElementId)
                else ElementId(int(link_revit_id))
            )
            instance_for_mirror = doc.GetElement(target_eid_for_mirror)
            # Unpin si nécessaire (sinon MirrorElements refuse).
            was_pinned = bool(getattr(instance_for_mirror, "Pinned", False)) \
                if instance_for_mirror else False
            if was_pinned and instance_for_mirror is not None:
                instance_for_mirror.Pinned = False
            from System.Collections.Generic import List as _NetList
            id_list = _NetList[ElementId]()
            id_list.Add(target_eid_for_mirror)
            # MirrorElements(doc, ids, plane, mirrorCopies=False) →
            # mirror in place (no copies created).
            ElementTransformUtils.MirrorElements(doc, id_list, plane, False)
            mirrored = True
            if was_pinned and instance_for_mirror is not None:
                # Re-pin après mirror.
                try:
                    instance_for_mirror.Pinned = True
                    final_pinned = True
                except Exception:  # noqa: BLE001
                    final_pinned = False
        except Exception:  # noqa: BLE001
            mirrored = False

    return {
        "file": str(path),
        "view_revit_id": view_revit_id,
        "link_revit_id": link_revit_id,
        "placement": placement,
        "mirrored": mirrored,
        "color_mode": color_mode,
        "aligned_to_view_origin": aligned_to_view_origin,
        "pinned": final_pinned,
    }


@tool(name="views_link_cad", tier=2)
def link_cad(
    kg: ProjectKG,
    doc: Any,
    file_path: str,
    view_revit_id: Optional[int] = None,
    placement: str = "origin",
    color_mode: str = "preserved",
    restore_pinned: bool = True,
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
        restore_pinned: si True (défaut), re-épingle le link après
            l'alignement sur view.Origin (préserve le comportement
            Revit standard où les liens CAD avec OrientToView sont
            verrouillés). Si False, laisse dépinglé — utile pendant
            la phase de validation visuelle où l'user veut peut-être
            ajuster manuellement.

    Returns:
        {"ok": bool, "file": str, "view_revit_id": int | None,
         "link_revit_id": int | None, "aligned_to_view_origin": bool,
         "pinned": bool}
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("File not found: {}".format(path))
    if placement not in ("origin", "center"):
        raise ValueError("placement must be 'origin' or 'center'")
    if color_mode not in ("preserved", "black_and_white", "by_layer"):
        raise ValueError("color_mode must be 'preserved', 'black_and_white', or 'by_layer'")
    if view_revit_id is None:
        raise ValueError(
            "view_revit_id required (no automatic ActiveView fallback "
            "in V0)."
        )

    if doc is None:
        return {
            "ok": True,
            "file": str(path),
            "view_revit_id": view_revit_id,
            "link_revit_id": None,
            "note": "doc is None — no Revit link created.",
        }

    from .. import revit_primitives as rp

    with rp.transaction(doc, "views.link_cad"):
        result = _link_cad_to_view(
            doc, path, view_revit_id, placement, color_mode, restore_pinned,
        )

    result["ok"] = True
    if result.get("link_revit_id") is None:
        result["note"] = (
            "Lien CAD posé. `link_revit_id` est None : la version "
            "PythonNet ne supporte pas le tuple-return — le lien existe "
            "côté Revit mais son id n'a pas été capturé."
        )
    return result


@tool(name="views_link_cad_many", tier=2)
def link_cad_many(
    kg: ProjectKG,
    doc: Any,
    links: List[Dict[str, Any]],
    placement: str = "origin",
    color_mode: str = "preserved",
    restore_pinned: bool = True,
) -> Dict[str, Any]:
    """Linke N DXF en **une seule** transaction Revit + un seul appel tool.

    Pattern bulk standard : évite N round-trips agent ↔ tool + N
    transactions Revit. Économie typique runtime 2026-05-13 P7 (8 DXF
    à linker) : 8 → 1 round-trip API, ~800 tokens économisés.

    Chaque entrée `links[i]` : `{file_path, view_revit_id}`. Les options
    `placement`, `color_mode`, `restore_pinned` sont appliquées à TOUS
    les links uniformément (use case : import projet où on linke tous
    les DXF avec les mêmes options).

    Transactionnel : si un link échoue, **aucun n'est commité** (rollback
    de la transaction Revit).

    Concepts: dxf, link, lien, bulk, batch, plusieurs, vue, view, phase 1
    Phrases: "lie tous les DXF", "batch link cad", "link many"
    Similar: views_link_cad, dxf_context_register_linked_view_many

    Args:
        links: liste de dicts `{file_path, view_revit_id}`. Chacun lié
            dans sa vue avec les options communes.
        placement, color_mode, restore_pinned: voir `views_link_cad`.
            Appliqués à TOUS les links.

    Returns:
        {"ok": bool, "count": int, "links": [{file, view_revit_id,
            link_revit_id, aligned_to_view_origin, pinned, ...}, ...]}
    """
    if not isinstance(links, list) or not links:
        raise ValueError("links must be a non-empty list")
    if placement not in ("origin", "center"):
        raise ValueError("placement must be 'origin' or 'center'")
    if color_mode not in ("preserved", "black_and_white", "by_layer"):
        raise ValueError("color_mode must be 'preserved', 'black_and_white', or 'by_layer'")

    # Pre-validate all entries (paths exist, view_revit_id given).
    normalized: List[Dict[str, Any]] = []
    for i, item in enumerate(links):
        if not isinstance(item, dict):
            raise ValueError(
                "links[{}] must be a dict, got {}".format(i, type(item).__name__)
            )
        fp = item.get("file_path")
        vid = item.get("view_revit_id")
        if not fp:
            raise ValueError("links[{}]: file_path required".format(i))
        if vid is None:
            raise ValueError("links[{}]: view_revit_id required".format(i))
        path = Path(fp)
        if not path.exists():
            raise FileNotFoundError(
                "links[{}]: file not found: {}".format(i, path)
            )
        normalized.append({
            "path": path,
            "view_revit_id": int(vid),
            "mirror_post_link": bool(item.get("mirror_post_link", False)),
        })

    if doc is None:
        return {
            "ok": True,
            "count": len(normalized),
            "links": [
                {
                    "file": str(n["path"]),
                    "view_revit_id": n["view_revit_id"],
                    "link_revit_id": None,
                    "note": "doc is None — no Revit link created.",
                }
                for n in normalized
            ],
        }

    from .. import revit_primitives as rp

    results: List[Dict[str, Any]] = []
    with rp.transaction(doc, "views.link_cad_many"):
        for spec in normalized:
            r = _link_cad_to_view(
                doc, spec["path"], spec["view_revit_id"],
                placement, color_mode, restore_pinned,
                mirror_post_link=spec["mirror_post_link"],
            )
            results.append(r)
    return {"ok": True, "count": len(results), "links": results}


# ----- views_override_element_colors_many ------------------------------
#
# Surcharge la couleur d'éléments dans une vue donnée (= "peint en
# rouge / jaune"). Use case principal : flagger visuellement les
# éléments suspects identifiés par `dwg_validate_import_3d`.


# Palette nommée → RGB.
_NAMED_COLORS = {
    "red": (255, 0, 0),
    "yellow": (255, 200, 0),
    "orange": (255, 128, 0),
    "green": (0, 200, 0),
    "blue": (0, 100, 255),
    "magenta": (255, 0, 255),
    "cyan": (0, 200, 255),
}


def _resolve_color(spec: Any) -> Tuple[int, int, int]:
    """Normalise une spec couleur en `(r, g, b)` int [0, 255].
    Accepte : nom (e.g. 'red'), `[r, g, b]`, `(r, g, b)`."""
    if isinstance(spec, str):
        key = spec.lower().strip()
        if key not in _NAMED_COLORS:
            raise ValueError(
                "Unknown color name {!r}. Known: {}".format(
                    spec, list(_NAMED_COLORS.keys()),
                )
            )
        return _NAMED_COLORS[key]
    if isinstance(spec, (list, tuple)) and len(spec) == 3:
        return tuple(int(max(0, min(255, c))) for c in spec)  # type: ignore[return-value]
    raise ValueError(
        "color must be a name str or [r, g, b], got {!r}".format(spec)
    )


def _find_solid_fill_pattern_id(doc: Any):
    """Cherche le pattern 'Solid fill' (drafting target) — nécessaire
    pour que les surfaces apparaissent VRAIMENT remplies de la couleur.
    """
    from Autodesk.Revit.DB import (
        FilteredElementCollector, FillPatternElement, FillPatternTarget,
    )
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            pattern = fp.GetFillPattern()
            if pattern is None:
                continue
            if pattern.IsSolidFill and pattern.Target == FillPatternTarget.Drafting:
                return fp.Id
        except Exception:  # noqa: BLE001
            continue
    return None


@tool(name="views_override_element_colors_many", tier=1)
def override_element_colors_many(
    kg: ProjectKG,
    doc: Any,
    items: List[Dict[str, Any]],
    view_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Surcharge la couleur d'affichage de N éléments dans une vue
    Revit (en **une seule** transaction).

    Use case principal : flagger visuellement les éléments suspects
    après `dwg_validate_import_3d` — rouge pour les certains-fantômes,
    jaune pour les sans-évidence-3D. L'override est par-vue : il ne
    modifie pas la couleur intrinsèque de l'élément, juste son
    affichage dans la vue cible.

    **Réversible** via `views_clear_element_overrides` (à venir) ou
    manuellement en Revit UI (Properties → Visibility/Graphics).

    Concepts: surcharge, override, couleur, color, peinture, paint,
              rouge, jaune, vert, jaune, flag, marquage, visuel,
              graphics, vue
    Phrases: "peins ces murs en rouge", "flag les suspects en jaune",
             "override color", "color these elements"
    Similar: dwg_flag_3d_suspects_in_view, dwg_validate_import_3d

    Args:
        items: liste de dicts `{llm_id, color}`. `color` peut être un
            nom (`"red"`, `"yellow"`, `"orange"`, `"green"`, `"blue"`,
            etc.) ou `[r, g, b]` (entiers 0-255). Les éléments sans
            binding Revit (= pas créés en Revit) sont skippés.
        view_ref: llm_id de la vue cible. Si None, utilise la vue
            active de Revit (`uidoc.ActiveView`).

    Returns:
        ``{"ok", "view_revit_id", "applied_count", "skipped_count",
            "skipped": [{llm_id, reason}, ...]}``
    """
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if doc is None:
        return {
            "ok": True, "view_revit_id": None,
            "applied_count": 0, "skipped_count": len(items),
            "skipped": [{"llm_id": it.get("llm_id"), "reason": "doc is None"}
                        for it in items],
            "note": "doc is None — no Revit overrides applied.",
        }

    # Pré-validation.
    specs: List[Dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError("items[{}] must be a dict".format(i))
        llm_id = item.get("llm_id")
        color = item.get("color")
        if not isinstance(llm_id, str) or not llm_id.strip():
            raise ValueError("items[{}]: llm_id required (str)".format(i))
        rgb = _resolve_color(color)
        specs.append({"llm_id": llm_id, "rgb": rgb})

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import (
        Color, ElementId, OverrideGraphicSettings,
    )

    # Résoud la vue cible.
    if view_ref is not None:
        if not kg.has_node(view_ref):
            raise ValueError("Unknown view_ref: {}".format(view_ref))
        view_rid_raw = kg.get_revit_id(view_ref)
        if view_rid_raw is None:
            raise ValueError(
                "view_ref {} has no Revit binding".format(view_ref)
            )
        view = doc.GetElement(ElementId(view_rid_raw))
    else:
        # Active view.
        # Note : `doc.ActiveView` retourne la dernière vue active du
        # document. Suffisant pour le pushbutton (l'user clique en
        # ayant la 3D ouverte typiquement).
        view = doc.ActiveView
        if view is None:
            raise ValueError(
                "No active view in document — pass view_ref explicitly."
            )

    view_revit_id = int(view.Id.Value)

    solid_fill_id = _find_solid_fill_pattern_id(doc)

    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    with rp.transaction(doc, "views.override_element_colors_many"):
        for spec in specs:
            llm_id = spec["llm_id"]
            r, g, b = spec["rgb"]
            try:
                eid_raw = kg.get_revit_id(llm_id)
            except Exception:  # noqa: BLE001
                eid_raw = None
            if eid_raw is None:
                skipped.append({"llm_id": llm_id, "reason": "no Revit binding"})
                continue
            try:
                elem = doc.GetElement(ElementId(eid_raw))
                if elem is None:
                    skipped.append({"llm_id": llm_id, "reason": "element not found"})
                    continue
                color = Color(r, g, b)
                ogs = OverrideGraphicSettings()
                ogs.SetProjectionLineColor(color)
                ogs.SetSurfaceForegroundPatternColor(color)
                if solid_fill_id is not None:
                    ogs.SetSurfaceForegroundPatternId(solid_fill_id)
                # Cut surface (visible dans les coupes).
                ogs.SetCutForegroundPatternColor(color)
                if solid_fill_id is not None:
                    ogs.SetCutForegroundPatternId(solid_fill_id)
                view.SetElementOverrides(ElementId(eid_raw), ogs)
                applied.append({
                    "llm_id": llm_id, "revit_id": eid_raw,
                    "rgb": [r, g, b],
                })
            except Exception as exc:  # noqa: BLE001
                skipped.append({
                    "llm_id": llm_id,
                    "reason": "{}: {}".format(type(exc).__name__, exc),
                })

    return {
        "ok": True,
        "view_revit_id": view_revit_id,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
    }


@tool(name="views_clear_element_overrides", tier=1)
def clear_element_overrides(
    kg: ProjectKG,
    doc: Any,
    llm_ids: Optional[List[str]] = None,
    view_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Reset les overrides graphiques d'éléments dans une vue donnée.

    Use case : après `views_override_element_colors_many`, nettoyer
    les couleurs pour repartir d'une vue neutre.

    Args:
        llm_ids: liste d'éléments à reset. Si None, reset TOUS les
            overrides actuellement présents dans la vue (= reset
            global de la vue).
        view_ref: llm_id de la vue. Si None, vue active.

    Returns:
        ``{"ok", "view_revit_id", "cleared_count"}``
    """
    if doc is None:
        return {"ok": True, "view_revit_id": None, "cleared_count": 0,
                "note": "doc is None — no-op."}

    from .. import revit_primitives as rp
    from Autodesk.Revit.DB import ElementId, OverrideGraphicSettings

    if view_ref is not None:
        if not kg.has_node(view_ref):
            raise ValueError("Unknown view_ref: {}".format(view_ref))
        view_rid_raw = kg.get_revit_id(view_ref)
        if view_rid_raw is None:
            raise ValueError(
                "view_ref {} has no Revit binding".format(view_ref)
            )
        view = doc.GetElement(ElementId(view_rid_raw))
    else:
        view = doc.ActiveView
        if view is None:
            raise ValueError("No active view; pass view_ref explicitly.")

    view_revit_id = int(view.Id.Value)
    empty = OverrideGraphicSettings()  # tous les fields par défaut = aucun override

    cleared = 0
    with rp.transaction(doc, "views.clear_element_overrides"):
        if llm_ids is None:
            # Reset tous les éléments du KG vivant dans la vue.
            from Autodesk.Revit.DB import FilteredElementCollector
            for elem in FilteredElementCollector(doc, view.Id):
                try:
                    view.SetElementOverrides(elem.Id, empty)
                    cleared += 1
                except Exception:  # noqa: BLE001
                    continue
        else:
            for llm_id in llm_ids:
                try:
                    eid_raw = kg.get_revit_id(llm_id)
                except Exception:  # noqa: BLE001
                    eid_raw = None
                if eid_raw is None:
                    continue
                try:
                    view.SetElementOverrides(ElementId(eid_raw), empty)
                    cleared += 1
                except Exception:  # noqa: BLE001
                    continue

    return {
        "ok": True,
        "view_revit_id": view_revit_id,
        "cleared_count": cleared,
    }
