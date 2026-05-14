"""dwg_reader.py — parsing DXF (et DWG via ODA File Converter) → entités normalisées.

§9 V0 Sem.4-5, UC1. Étape 1 du pipeline DWG ingest :

    file (.dxf | .dwg) → DwgEntity[]  ← ce module
                       → classify     (dwg_classifier.py)
                       → walls_create_many (tool dispatch)

**DXF directement** parsé par ezdxf (pure-Python).
**DWG via shell-out ODA File Converter** (utilitaire gratuit oda.org). Le
chemin est résolu par `config.oda_converter_path()` (file → env → glob).
Si DWG rencontré sans ODA installé, `ConfigError` actionnable.

**Coords en mètres** systématiquement. ezdxf expose `doc.units` (`$INSUNITS`)
qu'on convertit à la lecture pour que le caller travaille toujours en SI.

**Aucun import Revit** — ce module doit pouvoir tourner dans la venv locale
pour itérer hors-Revit sur les fixtures DXF.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import config
from .config import ConfigError


# ----- Unit conversion --------------------------------------------------
#
# ezdxf expose le code unit ($INSUNITS) qui suit la convention AutoCAD
# (0 = unitless, 1 = inch, 4 = mm, 5 = cm, 6 = m, ...). On stocke la table
# explicitement pour ne pas dépendre d'un mapping interne ezdxf qui
# pourrait évoluer.

_DXF_UNIT_TO_METERS: Dict[int, float] = {
    0: 1.0,         # unitless — caller may need to override
    1: 0.0254,      # inches
    2: 0.3048,      # feet
    3: 1609.344,    # miles
    4: 0.001,       # millimetres
    5: 0.01,        # centimetres
    6: 1.0,         # metres
    7: 1000.0,      # kilometres
    8: 25.4e-6,     # microinches
    9: 0.0254e-3,   # mils
    10: 0.9144,     # yards
}


def _unit_factor(insunits_code: Optional[int]) -> float:
    """m_per_dxf_unit. Renvoie 1.0 (passthrough) si le code est inconnu ou
    None — caller peut surcharger via `scale_override` à `parse`."""
    if insunits_code is None:
        return 1.0
    return _DXF_UNIT_TO_METERS.get(int(insunits_code), 1.0)


# ----- Entity model -----------------------------------------------------


@dataclass
class DwgEntity:
    """Représentation normalisée d'une entité DXF, agnostique du backend.

    `kind` : valeur DXF brute (LINE, LWPOLYLINE, ARC, INSERT, TEXT, …).
    `layer` : nom du layer (string, conservé tel que dans le fichier).
    `coords` : liste de points en mètres. Sémantique selon kind :
        - LINE : `[p1, p2]` (2 points 3D `[x, y, z]`).
        - LWPOLYLINE / POLYLINE : N sommets, ouvert ou fermé selon `attrs["closed"]`.
        - ARC / CIRCLE : centre + ([radius, start_angle_rad, end_angle_rad]).
        - INSERT (block ref) : point d'insertion + paramètres d'instance.
        - TEXT / MTEXT : point d'insertion (seul point utile pour le classifier).
    `attrs` : payload spécifique au kind (closed, radius, text content, …).
    """
    kind: str
    layer: str
    coords: List[List[float]] = field(default_factory=list)
    attrs: Dict[str, Any] = field(default_factory=dict)


# ----- Public parser ----------------------------------------------------


def parse(
    file_path: Path | str,
    *,
    scale_override: Optional[float] = None,
) -> Tuple[List[DwgEntity], Dict[str, Any]]:
    """Charge un fichier DXF (ou DWG via ODA) et renvoie ses entités normalisées.

    Args:
        file_path: chemin vers le fichier .dxf ou .dwg.
        scale_override: facteur multiplicateur m-per-dxf-unit à appliquer
            *en plus* de la conversion `$INSUNITS`. Utile quand le fichier
            est unitless (code 0) ou que les unités déclarées sont
            incorrectes. Par défaut None (utilise uniquement `$INSUNITS`).

    Returns:
        `(entities, meta)` où :
        - `entities` : liste de `DwgEntity` (coords toujours en mètres).
        - `meta` : `{units_code, units_factor_to_m, layers: [{name, color,
                     entity_count, kinds: {kind: count}}], total_entities,
                     dxf_version, source_format}`.

    Raises:
        FileNotFoundError: fichier inexistant.
        ConfigError: .dwg sans ODA File Converter résolu.
        ezdxf.DXFStructureError / DXFError: fichier DXF corrompu.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError("DWG/DXF file not found: {}".format(path))

    suffix = path.suffix.lower()
    if suffix == ".dwg":
        dxf_path = _dwg_to_dxf_via_oda(path)
        source_format = "dwg"
    elif suffix == ".dxf":
        dxf_path = path
        source_format = "dxf"
    else:
        raise ValueError(
            "Unsupported extension {}: expected .dxf or .dwg".format(suffix)
        )

    import ezdxf  # lazy : pas d'import au top pour rester importable sans ezdxf.

    doc = ezdxf.readfile(str(dxf_path))
    insunits = doc.header.get("$INSUNITS")
    base_factor = _unit_factor(insunits)
    factor = base_factor * (scale_override if scale_override is not None else 1.0)

    entities: List[DwgEntity] = []
    layer_counts: Dict[str, Dict[str, Any]] = {}

    for entity in doc.modelspace():
        try:
            converted = _convert_entity(entity, factor, doc=doc)
        except _SkipEntity:
            continue
        entities.append(converted)
        bucket = layer_counts.setdefault(
            converted.layer,
            {"name": converted.layer, "entity_count": 0, "kinds": {}, "color": None},
        )
        bucket["entity_count"] += 1
        bucket["kinds"][converted.kind] = bucket["kinds"].get(converted.kind, 0) + 1

    # Récupère la couleur de chaque layer (utile pour le LLM qui peut
    # discriminer murs porteurs / cloisons par couleur).
    for layer in doc.layers:
        bucket = layer_counts.get(layer.dxf.name)
        if bucket is not None:
            bucket["color"] = getattr(layer.dxf, "color", None)

    meta: Dict[str, Any] = {
        "units_code": insunits,
        "units_factor_to_m": factor,
        "layers": sorted(layer_counts.values(), key=lambda b: -b["entity_count"]),
        "total_entities": len(entities),
        "dxf_version": getattr(doc, "dxfversion", None),
        "source_format": source_format,
    }
    return entities, meta


# ----- DWG → DXF via ODA File Converter ---------------------------------


def _dwg_to_dxf_via_oda(dwg_path: Path) -> Path:
    """Convertit un .dwg en .dxf via l'utilitaire CLI ODA File Converter.

    Crée un répertoire temporaire pour input + output (l'ODA Converter
    opère par dossier, pas par fichier individuel — c'est sa CLI).
    Le DXF résultant survit l'appel via une copie dans un autre temp
    dir au lifetime étendu — voir `_persist_temp_dxf`.

    Args:
        dwg_path: chemin du fichier .dwg source.

    Returns:
        Path vers le .dxf généré (à parser ensuite).

    Raises:
        ConfigError: ODA File Converter non installé / non résolu.
        RuntimeError: la conversion échoue (return code ≠ 0).
    """
    oda_exe = config.oda_converter_path()
    if oda_exe is None:
        raise ConfigError(
            "DWG input detected but ODA File Converter is not installed.\n"
            "Install it (free) from https://www.opendesign.com/guestfiles/oda_file_converter,\n"
            "then either:\n"
            "  - drop the path into ~/.config/claude-in-revit/oda_converter_path.txt, or\n"
            "  - set the {} environment variable, or\n"
            "  - install to a standard location ({}).\n"
            "Alternatively, export your file as DXF from your CAD software.".format(
                config.ODA_ENV_VAR,
                config._ODA_DEFAULT_GLOBS[0],  # noqa: SLF001
            )
        )

    # ODA Converter signature CLI :
    #   ODAFileConverter <inDir> <outDir> <ACADver> <outFormat> <recurse> <audit> [<filter>]
    # ACADver = output AutoCAD version (ACAD2018 par défaut, compatible
    # avec ezdxf 1.x). outFormat = "DXF". recurse = 0. audit = 1.
    with tempfile.TemporaryDirectory(prefix="claude-in-revit-dwg-in-") as in_dir:
        in_path = Path(in_dir)
        staged = in_path / dwg_path.name
        shutil.copyfile(dwg_path, staged)
        out_dir = Path(tempfile.mkdtemp(prefix="claude-in-revit-dwg-out-"))
        # /!\ ODA Converter ne renvoie pas toujours un return code propre
        # — on vérifie la présence du DXF de sortie en plus du status.
        cmd = [
            str(oda_exe), str(in_path), str(out_dir),
            "ACAD2018", "DXF", "0", "1", "*.DWG",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=120, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "ODA Converter timed out (120s) on {}".format(dwg_path.name)
            ) from exc
        candidate = out_dir / (dwg_path.stem + ".dxf")
        if not candidate.exists():
            stderr = (result.stderr or b"").decode("utf-8", errors="replace")
            raise RuntimeError(
                "ODA Converter failed to produce {}.dxf "
                "(return {}). stderr: {}".format(
                    dwg_path.stem, result.returncode, stderr[:400],
                )
            )
        return candidate


# ----- Entity conversion (private) --------------------------------------


class _SkipEntity(Exception):
    """Raised inside `_convert_entity` to indicate the entity should be
    omitted from the output (unsupported kind, malformed coords, …)."""


def _block_bbox_2d(
    doc: Any, block_name: str, factor: float,
) -> Optional[Tuple[float, float]]:
    """Calcule le bbox 2D (XY) d'un bloc DXF à partir de ses entités de
    définition. Retourne `(width_m, depth_m)` en mètres post-conversion,
    ou None si le bloc est vide / introuvable / ne contient que des
    entités sans géométrie 2D extractible.

    Use case : déduire les dimensions de section d'un INSERT (e.g.
    une colonne HEA160 dessinée comme un H 16×15.2 cm dans le bloc)
    pour set les params `b`/`h` du FamilySymbol Revit dupliqué.

    Pris en charge : `LINE`, `LWPOLYLINE`, `POLYLINE`, `CIRCLE`, `ARC`,
    `INSERT` (nested block — récursion avec offset). Les autres entités
    (TEXT, HATCH, etc.) sont ignorées.
    """
    try:
        block = doc.blocks[block_name]
    except KeyError:
        return None
    except Exception:  # noqa: BLE001
        return None

    xs: List[float] = []
    ys: List[float] = []
    for e in block:
        try:
            kind = e.dxftype()
            if kind == "LINE":
                xs.extend([float(e.dxf.start.x), float(e.dxf.end.x)])
                ys.extend([float(e.dxf.start.y), float(e.dxf.end.y)])
            elif kind == "LWPOLYLINE":
                for x, y, *_ in e.get_points("xyb"):
                    xs.append(float(x)); ys.append(float(y))
            elif kind == "POLYLINE":
                for v in e.vertices:
                    xs.append(float(v.dxf.location.x))
                    ys.append(float(v.dxf.location.y))
            elif kind == "CIRCLE":
                cx, cy, r = float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)
                xs.extend([cx - r, cx + r])
                ys.extend([cy - r, cy + r])
            elif kind == "ARC":
                # Approximation : bbox = bbox du cercle plein. Sur-
                # estime pour les arcs partiels mais acceptable comme
                # majorant pour les profils de colonnes (qui sont
                # généralement convexes).
                cx, cy, r = float(e.dxf.center.x), float(e.dxf.center.y), float(e.dxf.radius)
                xs.extend([cx - r, cx + r])
                ys.extend([cy - r, cy + r])
            elif kind == "INSERT":
                # Bloc imbriqué : récursion avec décalage du point
                # d'insertion. Ignore rotation/scale du nested INSERT
                # (cas rare pour les blocs de colonnes ; à raffiner
                # si besoin).
                sub_bbox = _block_bbox_2d(doc, e.dxf.name, factor=1.0)
                if sub_bbox is not None:
                    ix, iy = float(e.dxf.insert.x), float(e.dxf.insert.y)
                    sw, sh = sub_bbox
                    xs.extend([ix, ix + sw])
                    ys.extend([iy, iy + sh])
        except Exception:  # noqa: BLE001
            continue

    if not xs:
        return None
    width = (max(xs) - min(xs)) * factor
    depth = (max(ys) - min(ys)) * factor
    return (width, depth)


def _convert_entity(entity: Any, factor: float, doc: Any = None) -> DwgEntity:
    """Dispatch sur le DXF type de l'entité. Lève `_SkipEntity` pour les
    kinds non supportés en V0 (HATCH, SPLINE, DIMENSION, IMAGE, …).
    """
    dxftype = entity.dxftype()
    layer = entity.dxf.layer

    if dxftype == "LINE":
        return DwgEntity(
            kind="LINE",
            layer=layer,
            coords=[
                _point_m(entity.dxf.start, factor),
                _point_m(entity.dxf.end, factor),
            ],
        )

    if dxftype == "LWPOLYLINE":
        # ezdxf LWPolyline expose .get_points("xyb") — (x, y, bulge).
        # On ignore le bulge (arcs paramétrés) en V0 : segments droits
        # uniquement. Caller post-segmente si besoin.
        pts = [
            _point_m_xy(x, y, factor)
            for x, y, *_ in entity.get_points("xyb")
        ]
        closed = bool(entity.closed)
        return DwgEntity(
            kind="LWPOLYLINE", layer=layer, coords=pts,
            attrs={"closed": closed},
        )

    if dxftype == "POLYLINE":
        # 2D ou 3D legacy POLYLINE — les vertex sont des sub-entities.
        pts = [_point_m(v.dxf.location, factor) for v in entity.vertices]
        closed = bool(entity.is_closed)
        return DwgEntity(
            kind="POLYLINE", layer=layer, coords=pts,
            attrs={"closed": closed},
        )

    if dxftype == "ARC":
        center = _point_m(entity.dxf.center, factor)
        radius = float(entity.dxf.radius) * factor
        start = float(entity.dxf.start_angle)  # degrés
        end = float(entity.dxf.end_angle)
        return DwgEntity(
            kind="ARC", layer=layer, coords=[center],
            attrs={
                "radius_m": radius,
                "start_angle_deg": start,
                "end_angle_deg": end,
            },
        )

    if dxftype == "CIRCLE":
        center = _point_m(entity.dxf.center, factor)
        radius = float(entity.dxf.radius) * factor
        return DwgEntity(
            kind="CIRCLE", layer=layer, coords=[center],
            attrs={"radius_m": radius},
        )

    if dxftype == "INSERT":
        # Block reference (porte, fenêtre, mobilier — typiquement).
        insertion = _point_m(entity.dxf.insert, factor)
        block_name = entity.dxf.name
        # Calcul bbox du bloc référencé (utile pour dimensions de
        # section dans Phase 2d colonnes). None si bloc vide/inconnu.
        block_bbox = (
            _block_bbox_2d(doc, block_name, factor) if doc is not None else None
        )
        attrs: Dict[str, Any] = {
            "block_name": block_name,
            "rotation_deg": float(entity.dxf.rotation),
            "scale": [
                float(entity.dxf.xscale),
                float(entity.dxf.yscale),
                float(entity.dxf.zscale),
            ],
        }
        if block_bbox is not None:
            attrs["block_bbox_m"] = list(block_bbox)
        return DwgEntity(
            kind="INSERT", layer=layer, coords=[insertion], attrs=attrs,
        )

    if dxftype in ("TEXT", "MTEXT"):
        insertion = _point_m(entity.dxf.insert, factor)
        text = entity.dxf.text if dxftype == "TEXT" else entity.text
        return DwgEntity(
            kind=dxftype, layer=layer, coords=[insertion],
            attrs={"text": str(text)},
        )

    raise _SkipEntity()


def _point_m(point: Any, factor: float) -> List[float]:
    """ezdxf Vec2/Vec3 → list[float] en mètres. Conserve z s'il existe."""
    x = float(point.x) * factor
    y = float(point.y) * factor
    z = float(getattr(point, "z", 0.0)) * factor
    return [x, y, z]


def _point_m_xy(x: float, y: float, factor: float) -> List[float]:
    """Helper pour LWPolyline (2D, z=0)."""
    return [float(x) * factor, float(y) * factor, 0.0]


# ----- Convenience: iterate by layer ------------------------------------


def entities_by_layer(
    entities: List[DwgEntity], layer_name: str,
) -> Iterator[DwgEntity]:
    """Filter helper : itère les entités d'un layer donné. Trivial mais
    rend les usages call-site lisibles (`for e in entities_by_layer(es, "WALL")`)."""
    for e in entities:
        if e.layer == layer_name:
            yield e
