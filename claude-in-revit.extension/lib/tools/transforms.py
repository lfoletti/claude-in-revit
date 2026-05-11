"""transforms.py — type-agnostic transformations on KG-managed Revit elements.

Six tools that operate on any subset of llm_ids regardless of element
type (Walls, Columns, Lines, …). They lean on
`Autodesk.Revit.DB.ElementTransformUtils` for the actual Revit work
(naturally type-agnostic) and on `kg_sync.ingest_revit_element` to
fold the resulting new/modified elements back into the KG.

Why generic instead of one set per type :
- ElementTransformUtils doesn't care if you pass it a Wall or a Column.
- `kg_sync.ingest_revit_element` dispatches to the right converter
  based on the element's class.
- Adding a new managed type (Doors, Windows, Rooms) means writing
  *one* converter + one `isinstance` branch in `ingest_revit_element`.
  All six transformations work on it automatically — zero new tools.

Token economy : the tools return `bulk_summary` (compact response with
contiguous llm_id range), and inputs are tiny (ids + a few numbers).
Building an N-element array used to require an N-item input list ;
now `elements_array_linear(ids, direction, count)` is constant-size.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ._helpers import bulk_summary
from ..llm_protocol import tool
from ..project_kg import ProjectKG


# ----- Validation helpers (Revit-free) ----------------------------------


def _validate_llm_ids(kg: ProjectKG, llm_ids: List[str]) -> None:
    if not isinstance(llm_ids, list) or not llm_ids:
        raise ValueError("llm_ids must be a non-empty list")
    for i, lid in enumerate(llm_ids):
        if not isinstance(lid, str) or not kg.has_node(lid):
            raise ValueError("llm_ids[{}]: unknown llm_id {!r}".format(i, lid))
        if kg.get_node(lid).get("deleted_at_turn") is not None:
            raise ValueError("llm_ids[{}]: {} is soft-deleted".format(i, lid))


def _require_bindings(kg: ProjectKG, llm_ids: List[str]) -> List[int]:
    """Resolve every llm_id to its Revit ElementId.Value (int), or raise."""
    raw: List[int] = []
    for i, lid in enumerate(llm_ids):
        eid = kg.get_revit_id(lid)
        if eid is None:
            raise ValueError(
                "llm_ids[{}]: {} has no Revit binding — run Refresh KG.".format(i, lid)
            )
        raw.append(eid)
    return raw


def _validate_vector(v: Any, name: str) -> List[float]:
    if not isinstance(v, list) or len(v) not in (2, 3):
        raise ValueError("{} must be [x, y] or [x, y, z] in metres".format(name))
    if len(v) == 2:
        return [float(v[0]), float(v[1]), 0.0]
    return [float(c) for c in v]


def _validate_point_2d(p: Any, name: str) -> List[float]:
    if not isinstance(p, list) or len(p) != 2:
        raise ValueError("{} must be [x, y] in metres".format(name))
    return [float(p[0]), float(p[1])]


# ----- Revit-side helpers (lazy imports) --------------------------------
#
# All `from Autodesk.Revit.DB import ...` happen inside the function
# bodies so this module stays importable under pytest.


def _build_elementid_collection(raw_ids: List[int]) -> Any:
    """Wrap a Python list of int element-id values into a typed
    `List[ElementId]` that `ElementTransformUtils.*` accepts. Using the
    .NET generic List explicitly is robust across PythonNet versions
    where auto-marshalling of Python lists to `ICollection<ElementId>`
    isn't always reliable."""
    from Autodesk.Revit.DB import ElementId
    from System.Collections.Generic import List as DotNetList
    out = DotNetList[ElementId]()
    for raw in raw_ids:
        out.Add(ElementId(raw))
    return out


def _refresh_kg_geometry(kg: ProjectKG, doc: Any, llm_ids: List[str]) -> None:
    """After an in-place Revit transform (translate/rotate), the KG
    geometry stored on each node is stale. Delegate to the central
    `kg_sync.refresh_node_from_revit` (which covers every supported
    node type — Walls, Columns, Doors, Windows, Lines — via the
    `_REFRESH_FIELDS` whitelist). Refs (level/type/host) are
    intentionally NOT touched by the helper, as transforms don't
    change them."""
    from .. import kg_sync
    for lid in llm_ids:
        kg_sync.refresh_node_from_revit(kg, doc, lid)


def _apply_translation_to_kg_node(
    kg: ProjectKG, llm_id: str, vec3: List[float],
) -> None:
    """Hors-Revit shortcut: shift the geometry attrs directly. Only
    used by `elements_translate(doc=None)` — sufficient for CLI / tests."""
    node = kg.get_node(llm_id)
    t = node.get("_type")
    if t == "Wall":
        p1 = node["p1"]; p2 = node["p2"]
        kg.modify_node(llm_id, {
            "p1": [p1[0] + vec3[0], p1[1] + vec3[1]],
            "p2": [p2[0] + vec3[0], p2[1] + vec3[1]],
        })
    elif t in ("ModelLine", "DetailLine"):
        p1 = node["p1"]; p2 = node["p2"]
        kg.modify_node(llm_id, {
            "p1": [p1[0] + vec3[0], p1[1] + vec3[1], p1[2] + vec3[2]],
            "p2": [p2[0] + vec3[0], p2[1] + vec3[1], p2[2] + vec3[2]],
        })
    elif t == "Column":
        pos = node["position"]
        kg.modify_node(llm_id, {
            "position": [pos[0] + vec3[0], pos[1] + vec3[1]],
        })


# ----- In-place transformations (no new elements) -----------------------


@tool(name="elements_translate", tier=1)
def translate(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
    vector: List[float],
) -> Dict[str, Any]:
    """Translate les éléments **en place** (pas de copie créée).

    Type-agnostique : fonctionne sur Walls, Columns, Lines, etc. en
    une seule transaction Revit. Les attributs géométriques du KG
    (p1/p2 pour murs et lignes, position pour poteaux) sont re-extraits
    depuis Revit après la translation pour rester cohérents.

    Concepts: translation, déplacement, move, décalage, offset, transformation
    Phrases: "déplace ces éléments de", "translate", "décale tout de",
             "shift these by"
    Similar: elements_copy, elements_rotate, walls_move

    Args:
        llm_ids: liste de llm_ids à déplacer (tous types confondus).
        vector: [dx, dy] ou [dx, dy, dz] en mètres.

    Returns:
        `bulk_summary(llm_ids)` — confirmation, ids inchangés.
    """
    _validate_llm_ids(kg, llm_ids)
    vec3 = _validate_vector(vector, "vector")

    if doc is None:
        for lid in llm_ids:
            _apply_translation_to_kg_node(kg, lid, vec3)
        return bulk_summary(llm_ids)

    raw_eids = _require_bindings(kg, llm_ids)
    from Autodesk.Revit.DB import ElementTransformUtils, XYZ
    from .. import revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    xyz = XYZ(
        rp.meters_to_internal(vec3[0]),
        rp.meters_to_internal(vec3[1]),
        rp.meters_to_internal(vec3[2]),
    )
    with rp.transaction(doc, "elements.translate"):
        ElementTransformUtils.MoveElements(doc, eid_coll, xyz)
        _refresh_kg_geometry(kg, doc, llm_ids)
    return bulk_summary(llm_ids)


@tool(name="elements_rotate", tier=1)
def rotate(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
    center: List[float],
    angle_deg: float,
) -> Dict[str, Any]:
    """Rotation **en place** autour d'un axe vertical à travers `center`.

    L'angle est en degrés (sens trigonométrique : positif = anti-horaire
    en vue plan). Pour un quart de tour horaire, `angle_deg = -90`.

    Concepts: rotation, pivote, tourne, rotate, axe
    Phrases: "fais pivoter de 45 degrés", "rotate by", "tourne autour de",
             "axe de rotation en (x, y)"
    Similar: elements_array_rotational, elements_mirror

    Args:
        llm_ids: liste de llm_ids à pivoter.
        center: [x, y] en mètres — point pivot (axe vertical).
        angle_deg: angle en degrés (trigonométrique, +ccw en plan).

    Returns:
        `bulk_summary(llm_ids)`.
    """
    _validate_llm_ids(kg, llm_ids)
    c2 = _validate_point_2d(center, "center")
    if not isinstance(angle_deg, (int, float)):
        raise ValueError("angle_deg must be a number")
    angle_rad = math.radians(float(angle_deg))

    if doc is None:
        raise ValueError(
            "elements_rotate requires a live Revit document — KG-only "
            "rotation is not supported in V0."
        )

    raw_eids = _require_bindings(kg, llm_ids)
    from Autodesk.Revit.DB import ElementTransformUtils, Line, XYZ
    from .. import revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    cx = rp.meters_to_internal(c2[0])
    cy = rp.meters_to_internal(c2[1])
    axis = Line.CreateBound(XYZ(cx, cy, 0.0), XYZ(cx, cy, 1.0))
    with rp.transaction(doc, "elements.rotate"):
        ElementTransformUtils.RotateElements(doc, eid_coll, axis, angle_rad)
        _refresh_kg_geometry(kg, doc, llm_ids)
    return bulk_summary(llm_ids)


# ----- Copy-producing transformations -----------------------------------


@tool(name="elements_mirror", tier=1)
def mirror(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
    plane_origin: List[float],
    plane_normal: List[float],
) -> Dict[str, Any]:
    """Crée une copie symétrique des éléments par rapport à un plan vertical.

    Le plan miroir est défini par un point d'origine `[x, y]` et un
    vecteur normal `[nx, ny]` dans le plan horizontal (le plan miroir
    lui-même est vertical, prolongeant cette ligne en Z).

    Concepts: symétrie, miroir, mirror, reflect, axe de symétrie
    Phrases: "symétrise par rapport à", "miroir le long de",
             "mirror about", "symétrie autour de l'axe"
    Similar: elements_rotate, elements_copy

    Args:
        llm_ids: liste d'éléments à symétriser.
        plane_origin: [x, y] — point sur l'axe de symétrie en plan.
        plane_normal: [nx, ny] — vecteur normal au plan miroir
            (perpendiculaire à l'axe visible en plan).

    Returns:
        `bulk_summary` avec les **nouveaux** llm_ids (les originaux
        restent).
    """
    _validate_llm_ids(kg, llm_ids)
    o = _validate_point_2d(plane_origin, "plane_origin")
    n = _validate_point_2d(plane_normal, "plane_normal")
    norm_len = math.sqrt(n[0] ** 2 + n[1] ** 2)
    if norm_len < 1e-9:
        raise ValueError("plane_normal must be non-zero")

    if doc is None:
        raise ValueError(
            "elements_mirror requires a live Revit document."
        )

    raw_eids = _require_bindings(kg, llm_ids)
    from Autodesk.Revit.DB import ElementTransformUtils, Plane, XYZ
    from .. import kg_sync, revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    ox, oy = rp.meters_to_internal(o[0]), rp.meters_to_internal(o[1])
    nx_, ny_ = n[0] / norm_len, n[1] / norm_len
    plane = Plane.CreateByNormalAndOrigin(
        XYZ(nx_, ny_, 0.0),
        XYZ(ox, oy, 0.0),
    )
    new_llm_ids: List[str] = []
    with rp.transaction(doc, "elements.mirror"):
        new_eids = ElementTransformUtils.MirrorElements(
            doc, eid_coll, plane, True,
        )
        for eid in new_eids:
            new_elem = doc.GetElement(eid)
            new_llm_ids.append(kg_sync.ingest_revit_element(kg, doc, new_elem))
    return bulk_summary(new_llm_ids)


@tool(name="elements_copy", tier=1)
def copy(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
    translation: List[float],
    rotation_angle_deg: float = 0.0,
    rotation_center: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Crée **une** copie des éléments avec translation (et option rotation).

    Combine translation + rotation en un seul `Transform`. Pour N
    copies, utiliser `elements_array_linear` ou `elements_array_rotational`.

    Concepts: copie, duplique, copy, dupliquer, transformation composée
    Phrases: "duplique avec décalage", "copie en tournant",
             "fais une copie à", "copy and translate"
    Similar: elements_array_linear, elements_mirror

    Args:
        llm_ids: éléments à copier.
        translation: [dx, dy] ou [dx, dy, dz] en mètres.
        rotation_angle_deg: angle de rotation autour de l'axe vertical
            (degrés, défaut 0).
        rotation_center: [x, y] pivot de rotation. Requis si
            `rotation_angle_deg != 0`, sinon ignoré.

    Returns:
        `bulk_summary` avec les llm_ids des nouvelles copies.
    """
    _validate_llm_ids(kg, llm_ids)
    t3 = _validate_vector(translation, "translation")
    if not isinstance(rotation_angle_deg, (int, float)):
        raise ValueError("rotation_angle_deg must be a number")
    has_rot = abs(rotation_angle_deg) > 1e-9
    if has_rot:
        if rotation_center is None:
            raise ValueError(
                "rotation_center required when rotation_angle_deg != 0"
            )
        c2 = _validate_point_2d(rotation_center, "rotation_center")
    else:
        c2 = [0.0, 0.0]

    if doc is None:
        raise ValueError("elements_copy requires a live Revit document.")

    raw_eids = _require_bindings(kg, llm_ids)
    from Autodesk.Revit.DB import (
        ElementTransformUtils, Line, XYZ,
    )
    from .. import kg_sync, revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    transl_xyz = XYZ(
        rp.meters_to_internal(t3[0]),
        rp.meters_to_internal(t3[1]),
        rp.meters_to_internal(t3[2]),
    )
    new_llm_ids: List[str] = []
    with rp.transaction(doc, "elements.copy"):
        # Phase A : pure translation via the XYZ overload of CopyElements
        # (rigid Transform overload doesn't accept composed
        # translation+rotation reliably across Revit versions — split avoids
        # `ElementTransformUtils.CopyElements` rejecting our payload).
        new_eids = ElementTransformUtils.CopyElements(
            doc, eid_coll, transl_xyz,
        )
        # Phase B : rotate the copies in place around the translated
        # pivot (i.e. the rotation_center plus the same translation we
        # just applied). Skip the call entirely if rotation_angle ≈ 0.
        if has_rot:
            new_coll = _build_elementid_collection(
                [int(eid.Value) for eid in new_eids]
            )
            pivot_x = rp.meters_to_internal(c2[0]) + transl_xyz.X
            pivot_y = rp.meters_to_internal(c2[1]) + transl_xyz.Y
            axis = Line.CreateBound(
                XYZ(pivot_x, pivot_y, 0.0),
                XYZ(pivot_x, pivot_y, 1.0),
            )
            ElementTransformUtils.RotateElements(
                doc, new_coll, axis,
                math.radians(float(rotation_angle_deg)),
            )
        for eid in new_eids:
            new_elem = doc.GetElement(eid)
            new_llm_ids.append(kg_sync.ingest_revit_element(kg, doc, new_elem))
    return bulk_summary(new_llm_ids)


@tool(name="elements_array_linear", tier=1)
def array_linear(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
    vector: List[float],
    count: int,
) -> Dict[str, Any]:
    """Crée un array linéaire de `count - 1` copies des éléments source.

    L'original n'est pas dupliqué — il fait position 0. Les copies
    sont placées aux décalages `1*vector`, `2*vector`, …,
    `(count-1)*vector`.

    Cas typique : trame régulière de poteaux à partir d'un seul,
    rangée de murs / mobilier, propagation. Pour `count=2`, équivalent
    à `elements_copy` avec une translation simple.

    Concepts: array, linéaire, série, duplication, replication, ligne
    Phrases: "duplique 10 fois en pas de 6", "array linéaire",
             "fais une rangée de", "replicate along"
    Similar: elements_copy, elements_array_rotational

    Args:
        llm_ids: éléments source.
        vector: [dx, dy] ou [dx, dy, dz] en mètres — décalage entre
            chaque position successive.
        count: nombre total de positions (≥ 2). 10 = 1 original + 9 copies.

    Returns:
        `bulk_summary` avec les nouveaux llm_ids (ordre :
        position 1 d'abord, puis 2, etc.).
    """
    _validate_llm_ids(kg, llm_ids)
    vec3 = _validate_vector(vector, "vector")
    if not isinstance(count, int) or count < 2:
        raise ValueError("count must be an integer ≥ 2 (got {!r})".format(count))

    if doc is None:
        raise ValueError(
            "elements_array_linear requires a live Revit document."
        )

    raw_eids = _require_bindings(kg, llm_ids)
    from Autodesk.Revit.DB import (
        ElementTransformUtils, Transform, XYZ,
    )
    from .. import kg_sync, revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    new_llm_ids: List[str] = []
    with rp.transaction(doc, "elements.array_linear"):
        for step in range(1, count):
            transl = XYZ(
                rp.meters_to_internal(vec3[0] * step),
                rp.meters_to_internal(vec3[1] * step),
                rp.meters_to_internal(vec3[2] * step),
            )
            transform = Transform.CreateTranslation(transl)
            new_eids = ElementTransformUtils.CopyElements(
                doc, eid_coll, transform,
            )
            for eid in new_eids:
                new_elem = doc.GetElement(eid)
                new_llm_ids.append(
                    kg_sync.ingest_revit_element(kg, doc, new_elem)
                )
    return bulk_summary(new_llm_ids)


def _kg_anchor_point(kg: ProjectKG, llm_id: str) -> List[float]:
    """Return the planar anchor of an element (midpoint for walls/lines,
    position for columns). 3-vector in metres."""
    node = kg.get_node(llm_id)
    t = node.get("_type")
    if t == "Wall":
        p1 = node["p1"]; p2 = node["p2"]
        return [(p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0, 0.0]
    if t in ("ModelLine", "DetailLine"):
        p1 = node["p1"]; p2 = node["p2"]
        return [
            (p1[0] + p2[0]) / 2.0,
            (p1[1] + p2[1]) / 2.0,
            (p1[2] + p2[2]) / 2.0,
        ]
    if t == "Column":
        pos = node["position"]
        return [float(pos[0]), float(pos[1]), 0.0]
    raise ValueError(
        "No planar anchor defined for KG type {!r}".format(t)
    )


def _kg_centroid(kg: ProjectKG, llm_ids: List[str]) -> List[float]:
    """Mean of element anchor points — in-plane centroid of the source set."""
    if not llm_ids:
        raise ValueError("Need at least one llm_id to compute centroid")
    sx = sy = 0.0
    for lid in llm_ids:
        a = _kg_anchor_point(kg, lid)
        sx += a[0]
        sy += a[1]
    n = len(llm_ids)
    return [sx / n, sy / n]


def _shorten_element_endpoints(element: Any, delta_m: float) -> None:
    """Move both endpoints toward the midpoint by `delta_m`. For Walls
    we mutate `LocationCurve.Curve`; for ModelCurve/DetailCurve we mutate
    `GeometryCurve`. No-op for elements without endpoints (Columns).

    Raises ValueError if the requested shortening would collapse the
    element to zero length or below.
    """
    from Autodesk.Revit.DB import (
        DetailCurve, Line, ModelCurve, Wall, XYZ,
    )
    from .. import revit_primitives as rp

    if isinstance(element, Wall):
        loc = element.Location
        curve = loc.Curve
    elif isinstance(element, (ModelCurve, DetailCurve)):
        loc = None
        curve = element.GeometryCurve
    else:
        return  # silently no-op (e.g. Column)

    a = curve.GetEndPoint(0)
    b = curve.GetEndPoint(1)
    midx = (a.X + b.X) / 2.0
    midy = (a.Y + b.Y) / 2.0
    midz = (a.Z + b.Z) / 2.0
    delta_ft = rp.meters_to_internal(delta_m)
    dx_a = midx - a.X; dy_a = midy - a.Y; dz_a = midz - a.Z
    len_a = math.sqrt(dx_a ** 2 + dy_a ** 2 + dz_a ** 2)
    # Each side moves by delta toward midpoint; the half-length must
    # exceed delta or we'd collapse / invert.
    if len_a <= delta_ft + 1e-9:
        raise ValueError(
            "Shortening by {:.3f} m on each side would collapse element "
            "of half-length {:.3f} m.".format(delta_m, len_a)
        )
    ua = (dx_a / len_a, dy_a / len_a, dz_a / len_a)
    new_a = XYZ(
        a.X + ua[0] * delta_ft,
        a.Y + ua[1] * delta_ft,
        a.Z + ua[2] * delta_ft,
    )
    dx_b = midx - b.X; dy_b = midy - b.Y; dz_b = midz - b.Z
    len_b = math.sqrt(dx_b ** 2 + dy_b ** 2 + dz_b ** 2)
    ub = (dx_b / len_b, dy_b / len_b, dz_b / len_b)
    new_b = XYZ(
        b.X + ub[0] * delta_ft,
        b.Y + ub[1] * delta_ft,
        b.Z + ub[2] * delta_ft,
    )
    new_line = Line.CreateBound(new_a, new_b)
    if loc is not None:
        loc.Curve = new_line
    else:
        element.GeometryCurve = new_line


@tool(name="elements_array_parametric", tier=1)
def array_parametric(
    kg: ProjectKG,
    doc: Any,
    src_llm_ids: List[str],
    count: int,
    per_step_translation: Optional[List[float]] = None,
    per_step_rotation_deg: float = 0.0,
    rotation_center_mode: str = "src_centroid",
    rotation_center: Optional[List[float]] = None,
    per_step_shortening_m: float = 0.0,
) -> Dict[str, Any]:
    """Array paramétrique avec transformations composées par itération.

    Une seule tool call qui couvre : translation, rotation locale,
    mutation géométrique (raccourcissement endpoints). À l'itération
    `i ∈ [1, count-1]`, applique cumulativement :
    - **translation** = `i × per_step_translation`
    - **rotation** de `i × per_step_rotation_deg` autour de
      `(src_centroid + i × per_step_translation)` si
      `rotation_center_mode = "src_centroid"`, sinon autour de
      `rotation_center`. Math équivalente implémentée :
      `Translation(i*v) ∘ RotationAround(src_centroid, i*angle)`.
    - **shortening** de `i × per_step_shortening_m` sur chaque endpoint
      du mur ou de la ligne (walls + lines uniquement, Columns ignorés).

    Cas typique : « duplique 9 fois en pas de 5 m, avec rotation de 9°
    par itération autour du centre, et raccourcissement de 10 cm par
    itération » → `count=10, per_step_translation=[5,0],
    per_step_rotation_deg=9, rotation_center_mode="src_centroid",
    per_step_shortening_m=0.10`. 1 tool call ≈ 80 tokens input, N×K
    nouveaux éléments en sortie compactée.

    Tout dans **une seule transaction Revit** — atomique. Si une
    itération échoue (longueur résultante ≤ 0, par exemple), tout
    rollback. Pas de demi-array.

    Concepts: array paramétrique, parametric, itération, composé, série,
              transformation, motif
    Phrases: "duplique N fois avec rotation et raccourcissement",
             "array paramétrique", "à chaque itération fais",
             "transformations incrémentales"
    Similar: elements_array_linear, elements_array_rotational, elements_copy

    Args:
        src_llm_ids: éléments source (tous types).
        count: nombre total de positions (≥ 2). L'original = position 0.
        per_step_translation: [dx, dy] ou [dx, dy, dz] en mètres,
            décalage par itération. Optionnel (défaut : pas de
            translation).
        per_step_rotation_deg: rotation incrémentale par itération
            (degrés). Défaut 0. Positif = anti-horaire en plan.
        rotation_center_mode: `"src_centroid"` (calcule le centre des
            éléments source une fois) ou `"fixed"` (centre explicite).
        rotation_center: [x, y] requis si mode = "fixed", sinon ignoré.
        per_step_shortening_m: raccourcissement de chaque endpoint par
            itération, en mètres. Défaut 0. Ignoré pour les Columns.

    Returns:
        `bulk_summary` avec les llm_ids des nouvelles copies (ordre :
        itération 1 d'abord, et au sein de chaque itération les copies
        dans l'ordre de `src_llm_ids`).
    """
    _validate_llm_ids(kg, src_llm_ids)
    if not isinstance(count, int) or count < 2:
        raise ValueError("count must be an integer ≥ 2 (got {!r})".format(count))
    translation = (
        _validate_vector(per_step_translation, "per_step_translation")
        if per_step_translation is not None
        else [0.0, 0.0, 0.0]
    )
    if not isinstance(per_step_rotation_deg, (int, float)):
        raise ValueError("per_step_rotation_deg must be a number")
    if rotation_center_mode not in ("src_centroid", "fixed"):
        raise ValueError(
            "rotation_center_mode must be 'src_centroid' or 'fixed' "
            "(got {!r})".format(rotation_center_mode)
        )
    has_rot = abs(float(per_step_rotation_deg)) > 1e-9
    if has_rot:
        if rotation_center_mode == "fixed":
            if rotation_center is None:
                raise ValueError(
                    "rotation_center required when rotation_center_mode='fixed'"
                )
            center_xy = _validate_point_2d(rotation_center, "rotation_center")
        else:
            center_xy = _kg_centroid(kg, src_llm_ids)
    else:
        center_xy = [0.0, 0.0]
    if not isinstance(per_step_shortening_m, (int, float)) or per_step_shortening_m < 0:
        raise ValueError("per_step_shortening_m must be a non-negative number")
    has_shorten = float(per_step_shortening_m) > 1e-9

    if doc is None:
        raise ValueError(
            "elements_array_parametric requires a live Revit document — "
            "the rotate + shorten ops need Revit's transform/mutation "
            "engines."
        )

    raw_eids = _require_bindings(kg, src_llm_ids)
    from Autodesk.Revit.DB import (
        ElementTransformUtils, Line, XYZ,
    )
    from .. import kg_sync, revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    cx_ft = rp.meters_to_internal(center_xy[0])
    cy_ft = rp.meters_to_internal(center_xy[1])
    new_llm_ids: List[str] = []
    with rp.transaction(doc, "elements.array_parametric"):
        for step in range(1, count):
            # Phase A : pure-translation copy (XYZ overload — reliable
            # across Revit versions). Composed Transform = translation +
            # rotation is rejected by `CopyElements(Transform)` in
            # Revit 2025 according to the API surface we hit.
            transl_x_ft = rp.meters_to_internal(translation[0] * step)
            transl_y_ft = rp.meters_to_internal(translation[1] * step)
            transl_z_ft = rp.meters_to_internal(translation[2] * step)
            transl_xyz = XYZ(transl_x_ft, transl_y_ft, transl_z_ft)
            new_eids = ElementTransformUtils.CopyElements(
                doc, eid_coll, transl_xyz,
            )

            # Phase B : rotate the freshly-translated copies in place,
            # around the *post-translation* centroid (centroid +
            # i × translation).
            if has_rot:
                new_coll = _build_elementid_collection(
                    [int(eid.Value) for eid in new_eids]
                )
                pivot_x = cx_ft + transl_x_ft
                pivot_y = cy_ft + transl_y_ft
                axis = Line.CreateBound(
                    XYZ(pivot_x, pivot_y, 0.0),
                    XYZ(pivot_x, pivot_y, 1.0),
                )
                ElementTransformUtils.RotateElements(
                    doc, new_coll, axis,
                    math.radians(float(per_step_rotation_deg) * step),
                )

            # Phase C : per-iteration geometric mutations (cumulative
            # shortening of endpoints toward midpoint), then KG ingest.
            shorten_this_step = float(per_step_shortening_m) * step
            for eid in new_eids:
                new_elem = doc.GetElement(eid)
                if has_shorten:
                    _shorten_element_endpoints(new_elem, shorten_this_step)
                new_llm_ids.append(
                    kg_sync.ingest_revit_element(kg, doc, new_elem)
                )
    return bulk_summary(new_llm_ids)


@tool(name="elements_array_rotational", tier=1)
def array_rotational(
    kg: ProjectKG,
    doc: Any,
    llm_ids: List[str],
    center: List[float],
    total_angle_deg: float,
    count: int,
) -> Dict[str, Any]:
    """Crée un array polaire (rotationnel) autour d'un axe vertical à
    travers `center`. `count` positions au total réparties sur
    `total_angle_deg` (l'original = position 0).

    Pour 12 éléments équidistants sur un cercle complet :
    `total_angle_deg = 360 × 11 / 12 = 330` (l'original couvre 0°, les
    11 copies vont de 30° à 330°).

    Concepts: array polaire, rotationnel, circulaire, cercle, polar
    Phrases: "12 poteaux en cercle autour de", "array polaire à 60°",
             "rotationally array"
    Similar: elements_rotate, elements_array_linear

    Args:
        llm_ids: éléments source.
        center: [x, y] en mètres — centre de l'array (axe vertical).
        total_angle_deg: angle parcouru entre la position 0 et la
            dernière, en degrés.
        count: nombre total de positions (≥ 2).

    Returns:
        `bulk_summary` avec les nouveaux llm_ids (positions 1 à count-1).
    """
    _validate_llm_ids(kg, llm_ids)
    c2 = _validate_point_2d(center, "center")
    if not isinstance(total_angle_deg, (int, float)):
        raise ValueError("total_angle_deg must be a number")
    if not isinstance(count, int) or count < 2:
        raise ValueError("count must be an integer ≥ 2 (got {!r})".format(count))

    if doc is None:
        raise ValueError(
            "elements_array_rotational requires a live Revit document."
        )

    raw_eids = _require_bindings(kg, llm_ids)
    from Autodesk.Revit.DB import (
        ElementTransformUtils, Transform, XYZ,
    )
    from .. import kg_sync, revit_primitives as rp

    eid_coll = _build_elementid_collection(raw_eids)
    cx = rp.meters_to_internal(c2[0])
    cy = rp.meters_to_internal(c2[1])
    pivot = XYZ(cx, cy, 0.0)
    z_axis = XYZ(0.0, 0.0, 1.0)
    step_rad = math.radians(float(total_angle_deg)) / (count - 1)
    new_llm_ids: List[str] = []
    with rp.transaction(doc, "elements.array_rotational"):
        for step in range(1, count):
            transform = Transform.CreateRotationAtPoint(
                z_axis, step_rad * step, pivot,
            )
            new_eids = ElementTransformUtils.CopyElements(
                doc, eid_coll, transform,
            )
            for eid in new_eids:
                new_elem = doc.GetElement(eid)
                new_llm_ids.append(
                    kg_sync.ingest_revit_element(kg, doc, new_elem)
                )
    return bulk_summary(new_llm_ids)
