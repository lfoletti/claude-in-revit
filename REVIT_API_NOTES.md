# Revit 2025 API — shopping list V0 tier-1

> Liste focalisée sur ce dont les tools tier-1 V0 vont avoir besoin
> (cf. design doc §5 et JOURNAL.md). Pas exhaustif — chaque entrée
> renvoie à revitapidocs.com pour le détail. Mise à jour à élargir
> en Semaine 1 V0 quand on touchera vraiment chaque API.
>
> **Convention URL** : `https://www.revitapidocs.com/2025/{guid}.htm`
> Quelques pages n'existent que sous `/2025.3/` (signalé `2025.3` quand c'est le cas).
> Le GUID est stable entre versions — un lien `/2025/...` redirige proprement même si la page a été ajoutée à la 2025.3.

## Phase 1 — Création d'un mur bout-en-bout

### `Document`
Classe pivot — `__revit__.ActiveUIDocument.Document` ou `revit.doc`. Tout passe par elle (collectors, transactions, Delete, NewFamilyInstance, NewRoom).
**Doc** : https://www.revitapidocs.com/2025/db03274b-a107-aa32-9034-f3e0df4bb1ec.htm
**Gotcha** : ne jamais conserver une instance Document entre deux external commands ; toujours la relire depuis `__revit__`.

### `Transaction`
Lifecycle obligatoire pour toute mutation : `tx.Start("nom") ... tx.Commit()` (ou `RollBack`). Le nom apparaît dans l'undo stack Revit.
**Doc** : https://www.revitapidocs.com/2025/308ebf8d-d96d-4643-cd1d-34fffcea53fd.htm
**Gotcha** : aucune écriture API ne fonctionne hors Transaction — `InvalidOperationException` direct. Préférer le wrapper pyrevit (cf. Phase PyRevit).

### `TransactionStatus`
Enum retourné par Start/Commit/RollBack : `Uninitialized`, `Started`, `RolledBack`, `Committed`, `Pending`, `Error`, `Proceed`. À comparer avant de poursuivre.
**Doc** : https://www.revitapidocs.com/2025/29b9a7a8-6754-8310-e063-622b569bb6d5.htm
**Gotcha** : `Committed` est aussi renvoyé pour une transaction vide — ne pas s'en servir comme proxy "il s'est passé quelque chose".

### `XYZ`
Point/vecteur 3D — constructeur `XYZ(x, y, z)`. Toutes coordonnées en **pieds Revit (internal units)**, pas en mètres.
**Doc** : https://www.revitapidocs.com/2025/c2fd995c-95c0-58fb-f5de-f3246cbc5600.htm
**Gotcha** : XYZ est immuable — `.Add`, `.Multiply`, etc. retournent un nouveau XYZ. Pas de set sur `.X`.

### `Line.CreateBound`
Crée une ligne bornée entre deux XYZ. C'est l'argument typique pour `Wall.Create(doc, curve, level, structural)`.
**Doc** : https://www.revitapidocs.com/2025/e7329450-434a-918b-661c-65e15e0585a5.htm (classe Line)
**Gotcha** : start ≠ end (sinon exception) ; longueur minimum ≈ 1/256 ft (Revit short-curve tolerance).

### `UnitUtils`
Conversions entre internal units (pieds) et display units. Méthodes : `ConvertFromInternalUnits(value, ForgeTypeId)`, `ConvertToInternalUnits(value, ForgeTypeId)`.
**Doc** : https://www.revitapidocs.com/2025/128dd879-fea8-5d7b-1eb2-d64f87753990.htm
**Gotcha** : depuis 2021 c'est `ForgeTypeId` (cf. `UnitTypeId`), plus `DisplayUnitType` (deprecated). Toujours convertir aux frontières I/O LLM.

### `UnitTypeId`
Constantes `ForgeTypeId` : `UnitTypeId.Meters`, `UnitTypeId.Millimeters`, `UnitTypeId.Feet`, `UnitTypeId.Degrees`, etc.
**Doc** : https://www.revitapidocs.com/2025/bc1b6454-f10a-66dc-9268-1dccbc403f78.htm
**Gotcha** : ce sont des propriétés statiques, pas un enum — `UnitTypeId.Meters` est un `ForgeTypeId`.

### `FilteredElementCollector`
Itérateur filtré sur le modèle. Chainable : `.OfClass(typeof(Wall)).WhereElementIsNotElementType().ToElements()`.
**Doc** : https://www.revitapidocs.com/2025/263cf06b-98be-6f91-c4da-fb47d01688f3.htm
**Méthodes clés** :
- `OfClass` — https://www.revitapidocs.com/2025/b0a5f22c-6951-c3af-cd29-1f28f574035d.htm
- `OfCategory(BuiltInCategory.OST_Walls)` — filtre par catégorie BIM
- `WhereElementIsNotElementType()` / `WhereElementIsElementType()` — séparer instances vs types
**Gotcha** : un collector sans aucun filtre lance `InvalidOperationException` à l'itération.

### `BuiltInCategory`
Enum géant. Ceux qui nous concernent V0 : `OST_Walls`, `OST_Levels`, `OST_Doors`, `OST_Windows`, `OST_Rooms`, `OST_Floors`, `OST_StructuralColumns`.
**Doc** : https://www.revitapidocs.com/2025/ba1c5b30-242f-5fdc-8ea9-ec3b61e6e722.htm
**Gotcha** : préfixe `OST_` toujours (Object Style Type, héritage AutoCAD). En IronPython : `from Autodesk.Revit.DB import BuiltInCategory`.

### `Wall` + `Wall.Create`
Méthode statique. Overload V0 cible : `Wall.Create(doc, curve, levelId, structural: bool)` (utilise le wall type par défaut).
**Doc classe** : https://www.revitapidocs.com/2025/b5891733-c602-12df-beab-da414b58d608.htm
**Doc overload (Curve, ElementId, Boolean)** : https://www.revitapidocs.com/2025/4a42066c-bc44-0f99-566c-4e8327bc3bfa.htm
**Gotcha** : `Wall.Create(doc, curve, wallTypeId, levelId, height, offset, flip, structural)` est l'overload "complet" si on a besoin de fixer la hauteur immédiatement — sinon passer par `WALL_USER_HEIGHT_PARAM` après création.

### `WallType`
Type (kind, width, layers). Récupérer via `FilteredElementCollector(doc).OfClass(WallType).FirstElement()` ou `doc.GetDefaultElementTypeId(ElementTypeGroup.WallType)`.
**Doc** : https://www.revitapidocs.com/2025/aa685433-b426-5e4f-bee1-e3487bb59518.htm
**Gotcha** : un projet vierge a au moins un WallType — fail rapide si zéro retour de la collecte.

### `Level`
Niveau (étage). Création : `Level.Create(doc, elevation_in_feet)`. Récupération : collector OfClass(Level).
**Doc classe** : https://www.revitapidocs.com/2025/577e5d4e-a558-118c-9dea-3b810b061775.htm
**Doc Create static** : https://www.revitapidocs.com/2025/d661b7cd-dec8-6ae6-a753-b14ac2568772.htm
**Gotcha** : `elevation` en pieds. `Level.Create` est l'API moderne ; `Document.Create.NewLevel` reste pour rétrocompat.

### `ElementId`
Identifiant unique dans un projet. Constructeur `ElementId(value: long)` ; `ElementId.InvalidElementId` pour "rien".
**Doc** : https://www.revitapidocs.com/2025/44f3f7b1-3229-3404-93c9-dc5e70337dd6.htm
**Gotcha** : depuis 2024 `Value` est un `long` (avant : `int IntegerValue`) — **breaking change** à ne pas oublier dans les bridges C#/Python. Ne pas persister un ElementId entre sessions Revit.

## Phase 2 — Reste des tier-1

### `FamilySymbol`
Type de famille (FamilySymbol = "type", FamilyInstance = "occurrence"). Avant placement : `if not symbol.IsActive: symbol.Activate(); doc.Regenerate()`.
**Doc** : https://www.revitapidocs.com/2025/a1acaed0-6a62-4c1d-94f5-4e27ce0923d3.htm
**Gotcha** : `NewFamilyInstance` échoue silencieusement (ou exception) si le symbol n'est pas actif. Toujours `Activate()` puis `Regenerate()`.

### `FamilyInstance`
Occurrence placée (porte, fenêtre, mobilier). Sortie de `Document.Create.NewFamilyInstance(...)`.
**Doc** : https://www.revitapidocs.com/2025/0d2231f8-91e6-794f-92ae-16aad8014b27.htm
**Gotcha** : pour porte/fenêtre, le hôte (mur) est obligatoire — sinon Revit les place "non hosted" et c'est moche.

### `Document.Create.NewFamilyInstance` (Creation.Document)
Plusieurs overloads. Pour porte/fenêtre dans un mur : `NewFamilyInstance(point, symbol, hostWall, structuralType)`.
**Doc overload (XYZ, FamilySymbol, Element, StructuralType)** : https://www.revitapidocs.com/2025/7febcfdb-dbfa-317a-1c5e-882621f3e846.htm
**Gotcha** : `StructuralType.NonStructural` pour portes/fenêtres ; le `point` est sur le mur, pas en l'air. Symbol doit être Active.

### `Architecture.Room`
Pièce (SpatialElement). Namespace `Autodesk.Revit.DB.Architecture`.
**Doc** : https://www.revitapidocs.com/2025/75c9d2c7-a402-ea8b-9e7c-f8bc3510bbd5.htm
**Gotcha** : une room a besoin d'un volume fermé par les murs/sols/toits ou d'une `Phase` valide pour être placée — sinon `unbounded`.

### `Document.Create.NewRoom`
Plusieurs overloads. Le plus simple V0 : `NewRoom(level, UV)` où UV est `(x_ft, y_ft)` 2D.
**Doc overload (Level, UV)** : https://www.revitapidocs.com/2025/28262c8c-d18a-338c-eb17-f406438949d8.htm
**Gotcha** : attention, le namespace `Creation.Document` (`doc.Create.NewRoom(...)`) n'est pas le même que `Architecture.Room.Create` — V0 on reste sur `doc.Create.NewRoom`.

### `ElementTransformUtils`
Translations / rotations / mirror sans toucher la géométrie raw. Statique.
**Doc** : https://www.revitapidocs.com/2025/82e737d5-fda4-bc10-6099-88999cd51300.htm
**Méthodes V0** :
- `MoveElement(doc, elementId, translationVectorXYZ)`
- `RotateElement(doc, elementId, axisLine, angleRadians)`
- `MirrorElement(doc, elementId, plane)` (et `MirrorElements` pour la collection)
**Gotcha** : `MoveElement` ne change PAS le Z d'un élément level-based (mur, instance hôte). Pour changer d'étage : reassign `WALL_BASE_LEVEL_PARAM`.

### `Document.Delete`
Deux overloads : `Delete(ElementId)` et `Delete(ICollection<ElementId>)`. Retourne la collection des ids effectivement supprimés (incluant dépendances).
**Doc (ElementId)** : https://www.revitapidocs.com/2025/a0461dd1-71d9-4581-1604-2ef8c211dd60.htm
**Doc (ICollection)** : https://www.revitapidocs.com/2025/f4ce9113-b164-954e-5025-7b4edbdcc07d.htm
**Gotcha** : supprime sans confirmation, même les pinned. Les dépendances (ex : porte hostée par mur) partent aussi — il faut tracker l'effet pour le KG.

### `Element.LookupParameter`
Trouve un Parameter par nom user-visible. Renvoie `null` si pas trouvé.
**Doc** : https://www.revitapidocs.com/2025/4400b9f8-3787-0947-5113-2522ff5e5de2.htm
**Gotcha** : non portable entre langues Revit (FR/DE/EN). Préférer `element.get_Parameter(BuiltInParameter.X)` quand le param est built-in.

### `Parameter`
Wrapper autour d'une valeur typée. Méthodes : `AsDouble()`, `AsString()`, `AsInteger()`, `AsElementId()`, `AsValueString()`. Setters : `Set(double)`, `Set(string)`, `Set(int)`, `Set(ElementId)`.
**Doc** : https://www.revitapidocs.com/2025/333ff41b-e6a7-d959-60bf-c3bfae495581.htm
**Gotcha** : toujours checker `param.StorageType` avant `AsDouble`/`AsString` — sinon exception. `Set` retourne `bool` (succès) — vérifier.

### `BuiltInParameter`
Enum géant des paramètres système. Pour V0 :
- `WALL_USER_HEIGHT_PARAM` — hauteur libre d'un mur (double, pieds)
- `WALL_BASE_LEVEL_PARAM` — niveau de base (ElementId)
- `WALL_BASE_OFFSET` — offset Z depuis le base level
- `INSTANCE_SILL_HEIGHT_PARAM` — hauteur d'allège fenêtre
- `INSTANCE_HEAD_HEIGHT_PARAM` — hauteur de linteau
- `ROOM_AREA`, `ROOM_NAME`, `ROOM_NUMBER`
- `ROOM_HEIGHT`, `ROOM_LOWER_OFFSET`
**Doc** : https://www.revitapidocs.com/2025/fb011c91-be7e-f737-28c7-3f1e1917a0e0.htm
**Gotcha** : la liste est énorme et incohérente. Quand on doute, ouvrir le param dans Revit, clic droit → "Show in Property Pane" puis chercher le label dans l'enum. Beaucoup de params ne sont accessibles que via `LookupParameter` (project params, shared params).

## Phase 3 — Transactions avancées

### `FailureHandlingOptions`
Configurable via `Transaction.GetFailureHandlingOptions()` puis `SetFailuresPreprocessor(IFailuresPreprocessor)` puis `Transaction.SetFailureHandlingOptions(opts)`.
**Doc** : https://www.revitapidocs.com/2025/c03bb2e5-f679-bf24-4e87-08b3c3a08385.htm
**Gotcha** : à appliquer **avant** `Commit()` — pas avant `Start()`. Sinon les warnings ouvrent un dialog modal qui bloque le tool LLM.

### `IFailuresPreprocessor`
Interface à implémenter (méthode `PreprocessFailures(FailuresAccessor)`) pour swallow / downgrade les warnings non-bloquants. Retourne `FailureProcessingResult.Continue`.
**Doc** : https://www.revitapidocs.com/2025/053c6262-d958-b1b6-44b7-35d0d83b5a43.htm (page sous /2025.3/, GUID stable)
**Gotcha** : utiliser `accessor.DeleteWarning(failure)` pour les warnings, pas `ResolveFailure` (qui essaie d'auto-fix). Pour les errors → renvoyer `FailureProcessingResult.ProceedWithRollBack`.

### `TransactionGroup`
Groupe plusieurs Transaction comme une unité atomique. Méthodes : `Start`, `Commit`, `RollBack`, `Assimilate` (fusionne en une seule entrée d'undo).
**Doc** : https://www.revitapidocs.com/2025/f1113d30-4c36-7844-1537-aad7f095cea0.htm
**Gotcha** : utile pour `@kg_synced` quand un tool fait N writes — `Assimilate` au commit pour ne laisser qu'un undo. `RollBack` annule TOUT (les transactions internes déjà committed sont retirées).

## PyRevit specifics

### Accès au Document depuis un script pyRevit
Trois patterns équivalents en haut d'un `script.py` :
```python
# 1. via les globals injectés par pyRevit
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
app = __revit__.Application

# 2. via le module pyrevit.revit (recommandé)
from pyrevit import revit
doc = revit.doc
uidoc = revit.uidoc
```
**Doc pyRevit** : https://docs.pyrevitlabs.io/reference/pyrevit/
**Gotcha** : `__revit__` n'existe que dans un script lancé par pyRevit. Pour les tests unitaires hors Revit, mocker via `lib/revit_planmaker/binding/_revit_stub.py`.

### `pyrevit.revit.Transaction` — context manager
Wrapper qui appelle `Start` à l'entrée, `Commit` à la sortie, et **`RollBack` automatique sur exception**. C'est ce qu'on veut pour `@kg_synced`.
```python
from pyrevit.revit.db.transaction import Transaction

with Transaction("create_wall_ABC123", doc, swallow_errors=False) as tx:
    wall = Wall.Create(doc, line, level.Id, False)
    # si raise → rollback auto, doc inchangé
```
**Source** : https://github.com/pyrevitlabs/pyRevit/blob/master/pyrevitlib/pyrevit/revit/db/transaction.py
**Doc** : https://docs.pyrevitlabs.io/reference/pyrevit/revit/db/transaction/
**Signature** :
```python
Transaction(name=None, doc=None, clear_after_rollback=False,
            show_error_dialog=False, swallow_errors=False,
            log_errors=True, nested=False)
```
**Gotcha** : par défaut `show_error_dialog=False` — bien pour un agent LLM, mais si une vraie erreur user survient elle sera silencieuse côté UI. Logger côté Python (`pyrevit.script.get_logger()`). Pour les transactions imbriquées, mettre `nested=True` (sinon Revit refuse).

### Alternative bas niveau (`Autodesk.Revit.DB.Transaction`) en Python
Si on a besoin de `FailureHandlingOptions` custom, faire à la main :
```python
from Autodesk.Revit.DB import Transaction, TransactionStatus

tx = Transaction(doc, "name")
tx.Start()
try:
    # ... mutations
    if tx.Commit() != TransactionStatus.Committed:
        raise RuntimeError("Commit failed")
except Exception:
    if tx.HasStarted() and not tx.HasEnded():
        tx.RollBack()
    raise
```
**Gotcha** : c'est exactement ce que `pyrevit.revit.Transaction` fait — préférer le wrapper sauf si on a besoin du contrôle fin (failure handler).
