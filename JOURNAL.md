# Journal de développement — Revit Planmaker

## Conventions d'entrée

- Une entrée par session de travail significative, datée `YYYY-MM-DD`.
- Sections dans cet ordre : **Contexte & objectif** → **Décisions** → **Phases**
  (une par bloc de travail, avec fichiers créés/modifiés et le pourquoi) →
  **Validation** → **État final & reste à faire**.
- Cross-références au design doc sous la forme `§N.M` (ex : §4.1 KG projet).
- Rapporter les bugs rencontrés avec leur **cause racine** et leur **fix**, pas
  juste « j'ai corrigé X ».
- Pas de copier-coller de code dans le journal — référer aux fichiers et
  expliquer la décision.

---

## 2026-05-13 (session u) — Cross-validation multi-source openings + Phase 2c sols

### Contexte & objectif

Runtime P7 session t : 7 fenêtres `DXF_WIN_60x95cm` créées sur les 4
coupes traversées, dimensions toutes identiques bien que P7 a en réalité
~10-15 fenêtres avec largeurs variables (2.00m / 2.10m). User : « les
dimensions des fenêtres ne sont pas correctes », puis successivement :
« la localisation et le nombre des fenêtres est à dériver des plans et
des élévations », « cross-validation largeur en élévation »,
« cross-validation allège et linteau via élévations », « nombre et
position cross-val élévation », « les sols pourraient être ajoutés ».

Objectif : refonte de Phase 2b vers une **source primaire plan**
(énumération exhaustive + position), avec cross-validation multi-source
(plan ↔ coupe ↔ élévation) pour chaque dimension. Puis Phase 2c sols.

### Décisions architecturales

**Sources par dimension** (validées par l'user) :

| Dimension | Source primaire | Cross-val | Tolérance |
|---|---|---|---|
| Nombre + position 2D | Plan (INSERT A-GLAZ) | Élévation (matching positionnel Phase 2b + comptage Phase 1) | 30cm (position) / 20% (count) |
| Largeur | Plan (bbox bloc, dim longue) | Élévation (bbox bloc) | 5cm |
| Hauteur | Élévation (bbox bloc, si plausible ≥ 0.30m) | Coupe (fallback) | — |
| Allège (sill) | Coupe (y_dxf − level_y) | Élévation (y_min local du bloc) | 5cm |
| Linteau (head) | Calculé (sill + height) | Élévation (y_max local) | 5cm |
| Profondeur (info) | Plan (bbox bloc, dim courte) | — | — |

Sur conflit : **on garde la source primaire** et on signale dans le
retour (`width_disagreement_cm`, `sill_disagreement_cm`, etc.).
Décision : non-bloquant — la modélisation continue, l'écart est
remonté au user pour décision.

**Sources pour Phase 2c sols** :
- Boundary : convex hull des Wall vivants du niveau (V1 simple).
- Épaisseur : paires de LINEs A-FLOR horizontales en coupe.
- Pas de toiture (skip dernier niveau par défaut).

### Phase A — Source primaire plan (énumération nombre + position)

`dwg_section_reader.py` :
- `read_plan_opening_inserts(entities) -> List[PlanOpening]` :
  analogue de `read_section_openings` mais pour plans. Position 2D
  directe (pas de `(0, level_y)` à résoudre via bbox).
- `read_plan_opening_dims_by_block_id(file)` retourne `{bid → {width_m,
  depth_m}}` (dim longue / dim courte de la bbox bloc).
- `read_elevation_opening_dims_by_block_id(file)` retourne `{bid →
  {width_m, height_m, sill_local_m, head_local_m}}`.
- Factorisé : `_extract_aglaz_bbox_per_block` retourne le 4-uple
  `(bbox_w, bbox_h, y_min, y_max)` consommé par les deux callers.

`dwg_import.py` (`_collect_plan_openings_world`) :

Nouveau collecteur primaire, remplace `_collect_coupe_openings_world`
dans `dwg_add_openings_to_walls_many`. Pipeline :

1. `_build_plan_dims_index(kg)` : agrège width+depth par bid sur tous
   les plans (max si conflit inter-plans).
2. `_build_elevation_dims_index(kg)` : idem pour élévations.
3. `_build_sill_index_from_coupes(kg, section_lines)` : `{(bid,
   level_elev) → {sill_m, height_m}}` depuis les coupes.
4. `_plan_path_to_level_elev(kg)` : mapping plan_path → level via
   `linked_views.view_name` ↔ `Level.name` (fallback regex `Niveau N`).
5. Énumère tous les `read_plan_opening_inserts` de chaque plan, level
   issu du mapping, enrichit width/depth (plan), height (élév fallback
   coupe), sill (coupe fallback default 0.9m fenêtre / 0.0m porte).

Couverture P7 : passe de **11 openings** (depuis coupes) à
**15 openings** (depuis plans : 5 × `255854` au N0, 5 × `255854` au
N1, 5 × `V1` au N1).

### Phase B — Cross-validation multi-source

`_collect_plan_openings_world` enrichi avec champs traçabilité :
`width_source`, `height_source`, `sill_source`, `width_disagreement_cm`,
`sill_disagreement_cm`, `head_disagreement_cm`,
`elev_seen_in: List[direction]`, `elev_position_disagreement_cm`.

Garde-fou sur bbox dégénérée : si `ed["width_m"] < 0.30m`, on ne fait
pas de cross-val (bbox sub-geom du bloc trop petite pour être
exploitable — observé pour `255854` et `V2` qui ont des blocs avec
~17mm de LINEs A-GLAZ visibles).

**Bug fix accents** : `parse_block_id` regex
`-([A-Za-z0-9_]+)-(?:Niveau|Coupe|Plan|Elevation)\b` ne matchait
**pas** `-V1-Élévation Est` (présence de l'accent). Étendu à
`[ÉE]l[ée]vation|Elevation`. Avant ce fix, `elev_dims` était vide pour
P7 → 0 match en cross-val.

`add_openings_to_walls_many` :
- Construit `_build_elevation_inserts_by_direction` (analogue à
  `_load_elevations_from_kg` mais avec `read_section_openings` +
  `resolve_section_opening_positions` pour avoir x_elev_abs des
  inserts A-GLAZ d'élévation).
- Pour chaque opening, après vote orientation : looks expected
  directions (EW → Nord/Sud, NS → Est/Ouest), `project_world_to_elevation`,
  matching INSERT plus proche (tol 30cm). Consume tracking pour
  identifier les INSERTs élév orphelins (sans plan correspondant).
- Stats `elevation_match_stats`, `elevation_orphans_per_direction`,
  `elevation_unmatched_plan_count`, `elevation_position_disagreements`.

### Phase C — Cross-val à Phase 1 audit

User : « je pensais que ce système de cross-validation était déjà
fonctionnel et actif depuis le check intégrité en phase 1 ». Lacune
reconnue.

`dwg_coherence.check_openings_plan_vs_elevation(plan_block_ids,
elevation_block_ids, plan_total, elev_total)` :
- Check ensembliste : block_id présent en plan absent en élévation
  (et inverse).
- Check comptage : écart total < 20% sinon warning.
- Severity : warnings non bloquant (décision user) — coherent avec
  `check_openings_matching` existant pour plan↔coupes.

Intégré dans `check_planset_integrity` (Phase 1) à côté des autres
checks. Sur P7 : flag `bid V2 missing in plan` (fenêtre en
élévation/coupe pas en plan) + `+73% écart count` (15 plan vs 26 élév
cumulés).

### Phase D — Garde-fou anti "ne coupent rien"

Runtime P7 session t : 4 erreurs Revit `Des occurrences de DXF_WIN_
210x99cm ne coupent rien` — `NewFamilyInstance` réussit mais la fenêtre
ne traverse pas son host wall (déborde du segment).

`add_openings_to_walls_many` : check pre-création
- `wall_len = hypot(p2-p1)`
- `pos_on_wall = clamp(t_proj, 0, 1) * wall_len`
- `if pos_on_wall - width/2 < 5cm or pos_on_wall + width/2 > wall_len -
  5cm` → skip + comptabiliser dans `openings_oversize_for_wall` +
  examples (jusqu'à 10).

Évite l'erreur Revit *avant* l'appel `Floor.Create` / `NewFamilyInstance`.

### Phase E — Tool `kg_reset_dxf_imports`

User entre deux itérations : la KG accumulait Wall/Window soft-delete-
absents (307 walls + 39 windows non-soft-delete sur 14 sessions). Les
windows reposaient sur des `host_wall_ref` pointant vers d'anciens
revit_id supprimés manuellement → `NewFamilyInstance(host=stale)` créait
des instances orphelines invisibles.

Tool `kg_reset_dxf_imports` (tier-2, `dwg_import.py`) :
- Soft-delete tous les Wall/Window/Door/Floor vivants du KG.
- Soft-delete les WallType `DXF_WALL_*`, FloorType `DXF_FLOOR_*`,
  FamilyType `DXF_WIN_*`/`DXF_DOOR_*`.
- Supprime côté Revit en une seule `rp.transaction`, enveloppée par
  `kg.transaction()` pour atomicité (rollback symétrique si commit
  Revit échoue).
- Mode `dry_run=True` : preview de l'inventaire sans mutation.
- Préserve : Level, Room, DxfImportContext, View.

### Phase F — Phase 2c sols complète

`dwg_section_reader.read_section_floor_slabs(entities)` :
- Collecte les LINEs A-FLOR horizontales (`|y2-y1| < 0.005m`).
- Apparie chaque ligne top avec sa ligne bot la plus proche (épaisseur
  0.05 - 0.60m) ayant un recouvrement horizontal.
- Retourne `SectionFloorSlab(top_y_m, bot_y_m, thickness_m, x_min_m,
  x_max_m)`. Sur P7 : 2 dalles/coupe (Niveau 0 + Niveau 1, 25cm
  partout, pas de toiture).

`tools/floors.py` :
- `floors_get_or_create_dxf_type[_many]` (tier-1) : duplique BasicFloor
  template + `cs.SetLayerWidth(struct_idx, thickness)`. Validation
  stale binding (analogue P7 session r pour walls). Pattern identique
  à `walls_get_or_create_dxf_type[_many]`.

`tools/dwg_import.py` :
- `_convex_hull_2d(points)` : Andrew's monotone chain.
- `_shoelace_area_2d(points)` : aire signée formule Gauss.
- `_slab_thicknesses_per_level(kg)` : agrège épaisseurs par niveau
  (max vote inter-coupes).
- `dwg_create_floors_many` (tier-2) : énumère niveaux, convex hull des
  Wall vivants par niveau pour le boundary, épaisseur depuis coupes,
  délègue à `floors_get_or_create_dxf_type_many` + `floors_create_many`.
- Option `skip_top_level` (défaut True) : pas de sol au niveau du
  sommet (= toiture, traitée séparément en phase ultérieure).
- Option `boundary_inflation_m` : dilatation isotrope du convex hull
  pour déborder les murs extérieurs (défaut 0).

### Validation

**Smoke tests** (KG.fn(doc=None) sur P7 ancien KG) :

- `_collect_plan_openings_world` : 15 openings énumérés (5 N0, 10 N1),
  width=2.10m partout (source `plan`).
- `_build_elevation_dims_index` après fix accents : 3 bids (`255854`,
  `V1`, `V2`) avec W=2.00m H=0.99m sill=0.76m head=1.75m.
- Cross-val largeur plan↔élév : 15 désaccords (10cm sur tous —
  vraisemblablement le plan inclut les débords d'allège, l'élévation
  ne mesure que le cadre).
- Cross-val sill : 15 matchs parfaits (coupe et élévation s'accordent
  à 0.7605m).
- `check_openings_plan_vs_elevation` Phase 1 : flag `V2 missing in
  plan` + `+73% écart count`.
- `read_section_floor_slabs` Coupes 1/2/3 : 2 dalles chacune,
  thickness=0.250m, top_y_m ∈ {0.00, 3.00}.
- `dwg_create_floors_many` : run dry-run sur KG ancien (paths
  obsolètes après renommage user des fichiers) → 0 floors (skip
  silencieux des fichiers inexistants — comportement correct).

**Validation runtime user** (Phase 2b après fix oversize) : 13
fenêtres créées + 2 rejetées (oversize for wall), 0 erreur Revit
"ne coupent rien". Vue 3D OK, types `DXF_WIN_210x99cm`.

### Bugs rencontrés + fix

1. **Double conversion d'unités** dans `_collect_plan_openings_world` :
   `dwg_reader.parse` retourne déjà les coords en mètres, mais je
   re-multiplie par `units_factor_to_m`. Résultat : toutes positions
   à `(0, 0)` au lieu de `(-15.49, 5.83)` etc. Fix : supprimer la
   conversion.

2. **`parse_block_id` ne matchait pas Élévation** (accent) — détaillé
   en Phase B. Sans ce fix, cross-val largeur/sill/head impossible
   pour P7.

3. **`elev_dims` retournait `{}`** alors que les coupes contiennent
   bien des INSERTs A-GLAZ : cause `parse_block_id` (bug 2). Trace
   inattendue : on lisait correctement les BLOCK_DEFINITIONs mais
   bid était None partout.

4. **Conversion Edit fantôme** : un Edit a généré une duplication
   de la signature `_collect_coupe_openings_world` (header dupliqué).
   Fix par Edit supplémentaire pour supprimer le doublon.

5. **`Set` import manquant** dans `floors.py` après extension Phase
   2c. Fix : ajout `Set` dans `from typing import`.

### État final

**Fichiers modifiés** (5) :
- `dwg_section_reader.py` : +`PlanOpening`, `read_plan_opening_inserts`,
  `read_section_floor_slabs`, `SectionFloorSlab`,
  `read_plan_opening_dims_by_block_id`,
  `read_elevation_opening_dims_by_block_id`,
  `_extract_aglaz_bbox_per_block`, fix regex accents.
- `dwg_coherence.py` : +`check_openings_plan_vs_elevation`.
- `tools/dwg_import.py` : +`_collect_plan_openings_world`,
  `_build_plan_dims_index`, `_build_elevation_dims_index`,
  `_build_sill_index_from_coupes`, `_plan_path_to_level_elev`,
  `_build_elevation_inserts_by_direction`, `_slab_thicknesses_per_level`,
  `_convex_hull_2d`, `_shoelace_area_2d`, `dwg_create_floors_many`
  (tier-2), `kg_reset_dxf_imports` (tier-2), garde-fou oversize dans
  `add_openings_to_walls_many`, intégration check Phase 1.
- `tools/floors.py` : +`floors_get_or_create_dxf_type[_many]` (tier-1).
- `prompt.pushbutton/script.py` : prompt système Phase 2a→2b→2c
  enchaînées.

**Reste à faire** :

- **Vraie reconstruction topologique du boundary sol** : convex hull
  marche pour P7 rectangle, fail pour plans en L / atrium / courette.
  Solution : parcourir le graphe des murs pour identifier le tour
  extérieur (algorithme face-tracing).
- **Filter automatique 100%** sur faux positifs murs : 14 tentatives
  V3.x toutes régressives (session t), encore à ouvrir avec un signal
  multi-source plus robuste (vote 3D + longueur min mur + linéarité).
- **Investigation `V2` manquant en plan P7** : remontée par cross-val
  Phase 1. À traiter au cas par cas (export Revit défaillant ?).
- **Toiture** : skip systématique en Phase 2c. Phase 2d à ouvrir si
  besoin (Roof.CreateBasic + slope + perimeter).
- **Phase 2.6 raffinements** : mappage des types DXF custom vers des
  types métier du template Revit (post-import par user, sous-tools à
  outiller).

---

## 2026-05-13 (session t) — V2 vote multi-sources complet : élévation + récupération orphans

### Contexte & objectif

Runtime P7 session s (V1) : 11 openings détectés, 0 hostées, 0 fusions
malgré le fix resolver de position. Cause : 4 openings tombent dans
des zones où le classifier walls plan n'a pas détecté de fragments
encadrants (murs extérieurs non classifiés, gaps trop grands).

User : « il faut passer direct à la validation par élévation et au
système de vote » + « les votes des coupes sont également pris en
compte n'est-ce pas ? ».

### Architecture vote multi-sources (V2)

**4 votants livrés** :

- `dwg_voting.aggregate_votes(votes, min_voters, threshold)` : somme
  pondérée par confidence, majorité gagne au-dessus du seuil.
- `dwg_elevation_reader.vote_wall_visible_in_elevation` : project mur
  plan → zone élévation (conv. cardinale P7), cherche A-WALL lines.
- `dwg_elevation_reader.vote_opening_visible_in_elevation` : cherche
  linteau (head_elev_y) + allège (sill_elev_y).
- `dwg_plan_openings.vote_wall_visible_in_section` : projection mur
  sur trait → x_cut → cherche section_wall correspondant.
- `dwg_plan_openings.vote_opening_visible_in_section` : matching
  block_id + proximité du trait.

**Convention élévation P7 calibrée** :
- Nord : x_elev = -X_world ; Sud : x_elev = +X_world
- Est : x_elev = +Y_world ; Ouest : x_elev = -Y_world
- Y_elev = Z_world (élévation absolue).

### Pipeline V2 intégré au tool

`dwg_import_walls_and_openings_typed_many` :

1. `_load_elevations_from_kg(kg)` : charge les 4 élévations depuis
   `linked_views` (filtrage par mots-clés filename).
2. Pipeline V1 existant : classify walls, merge_fragments via opening.
3. **Si host_idx=None après V1** → `_try_recover_orphan_via_vote` :
   construit mur virtuel via `build_virtual_wall_hypothesis`
   (perpendiculaire au trait, longueur 6m, épaisseur 20cm défaut),
   vote via 4 élévations, accept si majorité yes avec conf ≥ 0.5.
4. Nouveau champ retour : `openings_recovered_via_vote`,
   `elevations_loaded`.

### Validation runtime simulée sur P7 (offline)

Sur les section_lines + plans + coupes + élévations P7 :

```
Coupe openings detected: 11
Walls N0: 15, N1: 11
RECOVERED: op (-5.74, 5.83) lvl=3.0 via 2 yes votes conf=0.67
RECOVERED: op (-15.60, 1.36) lvl=0.0 via 4 yes votes conf=1.00
RECOVERED: op (-6.49, 1.36) lvl=0.0 via 2 yes votes conf=0.67
RECOVERED: op (-6.49, 1.36) lvl=3.0 via 2 yes votes conf=0.67

hosted_normal=7  via_vote=4  orphan_final=0
```

Toutes les openings hostées. Aucun orphan résiduel.

### Phases livrées

**Commit `cfa888a` — V2 step 1** : `dwg_voting.py` + `dwg_elevation_reader.py`
(parse + projection cardinale + `vote_wall_visible_in_elevation`).
17 tests.

**Commit `136d759` — V2 step 1bis** : votes coupes
(`vote_wall_visible_in_section`, `vote_opening_visible_in_section`) +
vote opening élévation. Architecture vote multi-sources complète.
6 tests.

**Commit ce commit — V2 step 2** : intégration au tool
(`_load_elevations_from_kg`, `_VirtualWall`, `_try_recover_orphan_via_vote`).
Pipeline complet avec récupération orphans.

### Validation cumulée

**597 tests verts** (574 → 597, +23). Pas de régression.

### À valider runtime sur P7

User supprime à nouveau les murs DXF + types côté Revit, re-prompt
« importe ce projet ». Attentes :
- `walls_merged_count` ≥ 0 (idéalement > 0, mais P7 ne semble pas
  avoir de fusions classiques).
- `openings_recovered_via_vote` ≈ 4 (cf. simulation offline).
- `openings_orphan_count` ≈ 0.
- 3D doit montrer les murs continus + fenêtres aux bonnes positions.

### Reste à faire

- Élargir le vote au filtre des faux positifs murs (paires parallèles
  = changement de matériau, pas mur réel). Cf. mémoire user 2026-05-13.
- Validation des fusions douteuses par élévation (downgrade fusions
  plan-only sans confirmation visuelle).
- Vote ouverture en élévation utilisé directement (pas juste pour mur
  virtuel) — confirmer porte vs fenêtre via linteau/allège.

---

## 2026-05-13 (session s) — V1 vote multi-sources : openings depuis coupe = source primaire

### Contexte & objectif

Runtime P7 session r : 2 problèmes constatés.

1. **Bug stale binding** : `walls_get_or_create_dxf_type_many` réutilisait
   un node KG dont le revit_id pointait sur un WallType supprimé →
   `Wall.Create` lève `ArgumentException: No WallType`. Fix commit
   `96d5fd9`.

2. **V0 plan-only inefficace** : `walls_merged=0` sur P7. L'algo se basait
   sur la présence d'INSERTs A-GLAZ en plan avec `width_m` parsable du
   block name. Sur Projet8 (P7), la convention de block name diffère ou
   les tolérances trop strictes empêchent toute fusion. Résultat : 26
   murs fragmentés + 8 fenêtres (sur murs intacts) + 7 orphelines +
   erreurs Revit « ne coupent rien » + warnings « doublons ».
   User : « je ne vois pas d'amélioration au niveau du modèle ».

User : « attaquer V1 vote multi-sources direct ».

### Décision : openings de la coupe = source primaire

Au lieu de partir des fragments en plan, on part des **INSERTs A-GLAZ
des coupes** :

- Chaque opening en coupe a `block_id`, `x_cut_m`, `sill_m`, `height_m`
  — données précises et fiables (export Revit AIA).
- La convention DXF section anchor (déjà résolue session m, mémoire
  `project-dxf-section-anchor-investigation`) projette `x_cut` en world
  plan via la `section_line` associée.
- Le plan ne sert plus qu'à détecter les fragments murs à fusionner
  *autour* de chaque opening projeté.

C'est l'incarnation V0 du framework de vote multi-sources discuté
session r : la coupe « vote » plus fortement que le plan pour la
position d'un opening parce qu'elle est plus discriminante.

### Phases livrées

**Extensions `lib/dwg_plan_openings.py`** (~200 lignes) :

- `CoupeOpening` (dataclass) : opening lu depuis coupe + projeté en world.
- `project_section_opening_to_world(x_cut, sl_p1, sl_p2)` : projection
  selon trait vertical (x_cut = world Y) ou horizontal (x_cut = world X).
- `find_host_wall_for_world_opening(opening_xy, walls, perp_tol)` :
  cherche le mur dont la centerline passe au plus près.
- `project_pos_onto_wall_centerline(pos, wall_p1, wall_p2, margin)` :
  clamp orthogonalement sur la centerline ± 5cm des extrémités. Évite
  l'erreur Revit `ArgumentException: ne coupent rien`.
- `merge_fragments_around_opening(walls, opening_xy, ...)` : pilotée
  par la position WORLD de l'opening (pas par INSERT A-GLAZ plan).
  Fusion sûre car la position vient de la source fiable.

**Refonte `dwg_import_walls_and_openings_typed_many`** (~250 lignes) :

- Helper `_collect_coupe_openings_world(kg, section_lines, ...)` : lit
  les openings de chaque coupe, projette en world, **déduplique** par
  `(block_id, position arrondie, niveau)` pour éviter les doublons quand
  une fenêtre apparaît dans 2 coupes (cause du warning Revit
  « occurrences identiques »).
- Pipeline V1 : lit section_lines (KG ou explicite via nouveau
  paramètre `section_lines`), collecte coupe_openings world, classify
  walls par plan, pour chaque opening trouve son plan via level
  elevation, fusion fragments, projet position sur centerline, crée
  walls + openings.
- Nouveau champ retour : `coupe_openings_detected` (nb d'openings
  distincts lus depuis les coupes après dédup).

### Tests

`tests/test_dwg_phase2.py` : refactor du smoke test V0 (qui passait
`coupe_paths` sans section_lines) → V1 layout cohérent (mur vertical
fragmenté, trait horizontal, opening coupe projeté pile dans le gap).
**574 tests verts** (pas de régression).

### Reste à valider runtime sur P7

L'user supprime à nouveau les 26 murs DXF + types côté Revit, puis
re-prompt « importe ce projet ». Attentes :
- `walls_merged_count > 0` (fusion via coupe).
- Aucune erreur Revit « ne coupent rien » (clamp centerline).
- Pas de doublons (dédup par block_id+position).
- Murs continus visibles en 3D + fenêtres hostées correctement.

### Reste à faire

- Audit `check_planset_integrity` toujours en mode severity-max. Les
  warnings « murs coupe sans contrepartie plan » et « openings
  unmatched » sont des faux positifs (murs intérieurs derrière le trait
  de coupe — normal). À reclasser comme info, pas warning. Pas urgent
  car le gate est `needs_user`, pas `abort`.
- Vote multi-sources complet (fusion plan + coupe + élévation) reste
  V2+. La V1 actuelle utilise coupe comme source primaire avec plan
  comme support de détection des fragments. Élévation pas encore
  intégrée (cf. mémoire `project-planset-coherence-byproduct`).

---

## 2026-05-13 (session r) — Phase 2.5 : fusion fragments + openings hostées

### Contexte & objectif

User runtime P7 après session q : 26 murs créés mais **discontinuités
3D** à chaque emplacement de fenêtre/porte. Cause : le classifier
plan voit une fenêtre dessinée par **interruption des 2 lignes du mur**
comme **2 fragments distincts**, sans regarder les INSERTs A-GLAZ.

User : « les openings devraient être évaluées en même temps pour
éviter la confusion entre une interruption du mur et la présence d'une
ouverture dans un mur continu ».

### Précisions runtime user (3 cas accumulés en session)

1. **Continuité = élévation/coupe** : un mur continu avec fenêtre
   montre en élévation linteau + allège ; avec porte, linteau seul.
   Une rupture pleine hauteur = vraie discontinuité.
2. **Trait d'interruption ≠ toujours opening** : peut indiquer fenêtre,
   porte, ou simple fin de mur. La plupart du temps porte/fenêtre,
   parfois rien.
3. **Paires parallèles ≠ toujours mur** : peuvent indiquer un
   changement de matériau (joint). Lecture élévation déterminante.

Décision V0 (cette session) : couvrir le cas 2 via présence/absence
d'INSERT A-GLAZ — algo plan-only. Les cas 1 et 3 nécessitent une
brique de validation élévation (V1, documentée en TODO dans le module).

### Phases livrées

**Module pur `lib/dwg_plan_openings.py`** (nouveau, ~300 lignes) :

- `_perp_distance_point_to_line` + `_project_param` + `_angles_close`.
- `MergedWall` (dataclass) : mur après fusion éventuelle, avec
  `source_indices` traçant les fragments d'origine.
- `AssignedOpening` (dataclass) : opening avec `host_wall_index`,
  `position_along_wall_m`, `reason` ∈ {merged_two_fragments,
  single_wall_intact, orphaned_no_match}.
- **`merge_walls_with_openings(walls, plan_openings, *, perp_tol_m,
  width_match_tol_m, angle_tol_rad)`** : algorithme en 3 passes
  - Passe 1 : pour chaque INSERT A-GLAZ avec width parsable, cherche
    2 fragments collinéaires dont le gap ≈ width. Si trouvé, fusion +
    opening hosted (`reason=merged_two_fragments`).
  - Passe 2 : ajoute les fragments non fusionnés comme murs intacts.
  - Passe 3 : pour les openings non assignés en passe 1, cherche un
    mur intact qui les contient (`reason=single_wall_intact`) ou les
    marque orphelins (`reason=orphaned_no_match`).
- **`classify_opening_kind(sill_m, height_m, …)`** : règle user
  (sill ≤ 0.15 ET height ≥ 1.9 → porte ; sinon fenêtre ; None →
  unknown).

**Tool `dwg_import_walls_and_openings_typed_many`** (`tools/dwg_import.py`,
tier-2) — orchestre Phase 2 complète walls+openings :

1. Pré-lit les openings des coupes (`coupe_paths` ou
   `DxfImportContext.section_lines`) indexés par `block_id` → `sill_m`,
   `height_m` (sill = `y_dxf - base_level_y`).
2. Pour chaque plan : classify walls + read A-GLAZ INSERTs +
   `merge_walls_with_openings`.
3. Dédup global thickness buckets → `walls_get_or_create_dxf_type_many`.
4. Build wall items globaux → `walls_create_many` (1 Tx).
5. Pour chaque opening assigné avec match coupe : classify door/window,
   construit l'item.
6. `openings_create_many` (1 Tx, mixte door+window).

Auto-détection des FamilyType Door/Window (1er de la catégorie dans
le KG) — overridable via `door_family_type_ref` / `window_family_type_ref`.

Openings sans match coupe → `openings_unmatched_count` (sill/height
inconnus, pas créés). Openings sans mur hôte trouvé →
`openings_orphan_count`.

**Prompt système** : la section Phase 2 pointe maintenant sur
`dwg_import_walls_and_openings_typed_many` plutôt que
`dwg_import_walls_typed_many`, avec note explicite sur le bug runtime
2026-05-13 (discontinuités). Le tool walls-only reste accessible si
l'user demande explicitement « pas d'openings ».

### Tests

15 nouveaux tests dans `tests/test_dwg_phase2.py` :
- Module pur (7) : perp_distance, classify door/window/unknown, merge
  no openings, merge fusion 2 fragments, no fusion sans width, no fusion
  si gap mismatch, opening sur mur intact.
- Tool smoke (2) : fusion + window créée, no coupe match → unmatched.

### Validation

**574 tests verts** (564 → 574, +15, comprend les autres petits ajouts
incidentaux). Pas de régression.

### TODO V1 (documentés)

- `validate_merge_via_elevation_or_section` : downgrade fusions plan-only
  sans confirmation visuelle (linteau/allège attendus).
- Filtre faux positifs murs (paires parallèles = changement de matériau).
- Détection hauteurs murets vs pleine hauteur depuis élévation.
- Brique commune : `validate_via_elevation(walls, elevation_entities,
  direction, level_elevations)`.

### Reste à faire Phase 2

6. **Sols** Floor avec FloorTypes custom DXF_FLOOR_<cm>cm (pattern
   identique aux walls, à livrer en session s).
7. **Vue 3D** déjà OK via `views_open_3d`.

### Cleanup P7 attendu (user)

User va supprimer les 26 murs DXF_WALL_*cm de P7 (et types) avant de
re-prompter « importe ce projet » avec le nouveau pipeline. Le KG
soft-delete les anciens nodes ; les nouveaux types DXF_WALL_*cm créés
réutilisent les noms (idempotent côté KG, mais Revit aura un conflit
de nom si le type Revit n'a pas été supprimé — à voir runtime).

---

## 2026-05-13 (session q) — Lever l'interruption Phase 1 → Phase 2 dans le prompt système

### Contexte & objectif

User signale runtime sur P7 : « aucun mur créé ». Phase 1 complète
(audit + setup + 3D), mais l'agent S'ARRÊTE et propose à l'user de
« relancer pour Phase 2 », au lieu d'enchaîner directement.

User : « as-tu levé l'interruption du pipeline ? » → moi : interprété
à tort comme une question sur le hard-gate côté code. Rectifié :
l'« interruption » désigne la non-continuité Phase 1 → Phase 2 dans
le même tour de conversation.

### Cause racine

Le prompt système (`prompt.pushbutton/script.py` ligne 374 avant fix)
contenait une instruction explicite **« IMPORT PROJET — STOP après
Phase 1 (setup uniquement) ... NE CRÉE PAS DE MURS ... propose qu'il
prompte à nouveau pour Phase 2 »**. Cette instruction date de session
m (quand Phase 2 n'était pas encore livrée). Les sessions o (audit) et
p (extract + types + import_typed[_many]) ont livré les tools Phase 2
mais **n'ont pas mis à jour le prompt système** — donc l'agent suit
fidèlement la consigne et stoppe.

### Fix

Réécriture du bloc IMPORT PROJET dans `_STATIC_SYSTEM_PROMPT`. Nouveau
flow explicite :

```
1. check_planset_integrity(directory)         ← gate
   ├─ gate=abort       → stoppe, présente errors à l'user
   ├─ gate=needs_user  → ui_confirm_yes_no warnings, puis continue
   └─ gate=pass        → enchaîne directement
2. Phase 1 : inspect + traits + levels reconcile (dialog GROUPÉ) +
   views_create_section_many + views_link_cad_many (tous DXF) +
   dxf_context_register_*_many
3. Phase 2 : dwg_extract_wall_thicknesses_many (info pour résumé) +
   dwg_import_walls_typed_many (1 appel pour tous les plans)
4. views_open_3d + résumé consolidé
```

Points-clés du nouveau prompt :
- **Gate explicite** avec 3 branches selon `gate_status`.
- **Pas de confirmation préalable sur la distribution d'épaisseurs**
  (Phase 2 enchaîne directement — l'user a déjà demandé l'import).
- **Bulks obligatoires** listés explicitement (économies tokens).
- **Exception niveaux** maintenue (dialog GROUPÉ après
  `levels_reconcile_with_dxf`).
- L'instruction « propose qu'il prompte à nouveau pour Phase 2 » a
  disparu.

### Validation

**564 tests verts** (pas de régression — le prompt système n'est pas
testé directement). À valider runtime sur P7 :

- Prompt « importe ce projet C:\... » devrait enchaîner Phase 1
  complète + Phase 2 (murs N0 + N1 + types DXF_WALL_*cm) dans un
  seul tour.
- Si l'user veut explicitement stopper après Phase 1 (« inspecte
  seulement »), le wording le permet — c'est l'INTENT user qui dicte,
  pas une consigne hardcodée.

### Reste à faire / dette ouverte

- **Hard-gate code-side** : actuellement soft (basé sur compliance
  LLM avec `ok=False`). Pour un vrai blocage côté code, il faudrait
  persister `gate_status` dans le `DxfImportContext` + helper
  `_assert_dxf_gate_open(kg)` à brancher dans les tools mutants +
  tool `dxf_gate_release(reason)`. Pas urgent vu que le soft suffit
  en pratique (LLM bien instruit respecte `ok=False`). À refaire si
  un incident runtime se produit.

### Méta : leçon de session

**Prompt système et tools livrent ensemble**. Quand on livre un
nouveau scope fonctionnel (ici Phase 2), il faut mettre à jour
**à la fois** les tools ET le prompt système qui les orchestre.
Sinon les tools sont disponibles mais inertes — c'est exactement ce
qui s'est passé runtime. Ajout à la checklist mentale : « est-ce
que le prompt système connaît mes nouveaux tools ? est-ce qu'il
dit à l'agent quand les utiliser ? ».

---

## 2026-05-13 (session p) — Phase 2 étapes 2-4 : extract + types custom + import typed

### Contexte & objectif

Session p directement après validation runtime de session o sur P7
(Phase 1 + audit `check_planset_integrity` OK). User : « ok vas-y ».
Objectif : livrer les 3 micro-étapes Phase 2 restantes côté creation :

- **Étape 2** : détection des épaisseurs uniques observées dans le plan.
- **Étape 3** : création des `WallType` custom `DXF_WALL_<cm>cm`.
- **Étape 4** : création des murs en mappant chaque épaisseur à son type.

Source de vérité : mémoire `project-phase2-custom-types` (« créer des
types custom sans matching des types existants ; user refine après »).

### Phases livrées

**Étape 2 — `dwg_extract_wall_thicknesses`** (`tools/dwg_import.py`,
tier-2). Preview seul : classify plan → distribution par bucket cm
(configurable via `bucket_cm`). Output : `thickness_buckets: [{cm,
count, type_name, wall_indices}]`. Read-only — sert à l'agent à
présenter au user avant import.

**Étape 3 — `walls_get_or_create_dxf_type` + `_many`**
(`tools/walls.py`, tier-1).

- `_dxf_wall_type_name(thickness_m, bucket_cm)` : construit le nom
  canonique `DXF_WALL_<cm>cm`.
- `_find_dxf_wall_type_in_kg(kg, target_name)` : lookup KG (idempotence).
- `_find_simple_basic_wall_type(doc)` : auto-détection d'un BasicWall
  template à dupliquer. Préférence : 1-layer (plus prévisible à
  ajuster). Fallback : 1er BasicWall trouvé.
- `_create_dxf_wall_type_in_revit(doc, base, name, thickness)` :
  `Duplicate` + `GetCompoundStructure` + ajustement de la layer Core
  (ou layer 0 si 1 layer unique). Doit être appelé dans une
  transaction outer.
- Tool unitaire `walls_get_or_create_dxf_type` : idempotent par
  recherche KG sur le nom canonique. KG-only fallback (doc=None) crée
  juste le node WallType.
- Bulk `_many` : dédup interne sur buckets, 1 Tx Revit pour tous les
  types non-existants. Coding policy bulk respectée d'emblée.

**Étape 4 — `dwg_import_walls_typed`** (`tools/dwg_import.py`,
tier-2). Orchestre :

1. Classify plan → WallCandidates.
2. Bucket épaisseurs uniques.
3. Délègue à `walls_get_or_create_dxf_type_many` (Tx 1).
4. Build items avec mapping `bucket_cm → wall_type_ref`.
5. Délègue à `walls_create_many` (Tx 2).
6. Sort `{walls_imported, types_created, types_reused,
   thickness_distribution, types, inner_walls}`.

**Atomicité** : 2 transactions Revit séparées (types puis murs). Pas
atomique entre les deux mais idempotent — si la création des murs
échoue, les types restent et sont réutilisés au prochain run. Pas
critique car la création des murs ne modifie pas les types existants.

### Tests

**Nouveau `tests/test_dwg_phase2.py`** — 17 tests. Couverture :

- `dwg_extract_wall_thicknesses` (2) : 3 buckets, bucket_cm=5 merge.
- `walls_get_or_create_dxf_type` (4) : KG create, idempotence,
  bucket cm name, reject négatif.
- `walls_get_or_create_dxf_type_many` (2) : dédup, reuse.
- `dwg_import_walls_typed` (4) : roundtrip 3 épaisseurs, idempotence
  re-import, level_ref inconnu rejeté, refuse DXF section.
- `dwg_extract_wall_thicknesses_many` (2) : agrégation globale
  multi-plans, reject empty list.
- `dwg_import_walls_typed_many` (3) : dédup types globale entre 2
  plans (20cm partagé → 1 seul type créé), reject level inconnu,
  reject empty items.

### Bulks ajoutés (réaction user « ces functions sont-elles bulkables »)

Cohérent avec la coding policy `feedback-bulk-tool-variant-policy` :
- `dwg_extract_wall_thicknesses_many(file_paths)` — extraction multi-plans
  avec `global_distribution` dédupliquée pour faciliter l'enchaînement.
- `dwg_import_walls_typed_many(items)` — import bulk avec **dédup
  global des buckets** entre plans : un mur de 20cm dans N0 et N1 →
  1 seul `DXF_WALL_20cm` partagé. Gain : 2 Tx Revit au lieu de 2N,
  1 round-trip API au lieu de N.

### Validation

**564 tests verts** (547 → 564, +17). Pas de régression.

### Flow d'import end-to-end (à valider runtime sur P7)

```
1. check_planset_integrity(directory)          ← gate (session o)
2. dwg_inspect_sections + find_section_markers + ...  ← Phase 1
3. levels_reconcile_with_dxf → levels_create_many (si besoin)
4. views_create_section_many + views_link_cad_many
5. (NEW) dwg_extract_wall_thicknesses_many(file_paths=[N0, N1])  ← preview
6. (NEW) ui_confirm_choices                    ← user valide distribution
7. (NEW) dwg_import_walls_typed_many(items=[                   ← création
       {file_path: N0, level_ref: niveau_0, height_m: 3},
       {file_path: N1, level_ref: niveau_1, height_m: 3},
   ])
8. views_open_3d                               ← validation visuelle
```

Pour P7 (2 plans) : 1 appel `_many` au lieu de 2 appels lockstep =
1 round-trip API + 2 Tx Revit (au lieu de 4) + types factorisés.

### Reste à faire Phase 2

5. **Ouvertures** sill/head depuis coupes/élévations.
6. **Sols** Floor avec FloorTypes custom DXF_FLOOR_<cm>cm
   (pattern identique aux walls — pourrait être livré en session q).
7. **Vue 3D** déjà couverte par `views_open_3d` existant.

---

## 2026-05-13 (session o) — Phase 2 étape 1 + Audit d'intégrité du plan set

### Contexte & objectif

Démarrage Phase 2 après Phase 1 production-ready. User : « nouvelle
session pour la phase 2 ». Spec Phase 2 spec'd dans la mémoire
`project-phase2-custom-types` (7 micro-étapes). Étape 1 = recoupement
murs plan ↔ coupes.

**Pivot en cours de session** : user a re-cadré : « un "audit
d'intégrité" du plan set est livré en 1, avant de proposer des
changements dans le modèle, notamment les niveaux ». Donc le sous-
produit `check_planset_coherence` (initialement futur) devient
**`check_planset_integrity`** livré dans la même session, comme **étape
1 du flow d'import**. Le recoupement walls (Phase 2.1) devient une
brique de cet audit.

Cadrage user complet :

- Test runtime sur P7 (asymétrique, déjà calibré).
- Mismatch walls : **présenter à l'user** pour décision.
- Intersection : **stricte segment↔segment**.
- Audit scope : tout (source, scale, levels coupes, walls, openings).
- Audit gate : **hard gate** (errors → `ok=False`, agent doit stopper).
- Dossier incomplet : si seulement plan ou plan+1 coupe → audit
  dégradé (warnings, pas errors), continuer possible.
- Fichiers `unknown` : ignorer silencieusement (pas de warning).

### Décisions

- **Architecture en 2 modules purs** :
  - `dwg_section_reader.read_section_walls()` réutilise le classifier
    existant (paires parallèles) et filtre les paires verticales sur
    A-WALL → `SectionWall(x_cut_m, thickness_m, y_bottom_m, y_top_m)`.
    Pas besoin d'écrire un nouveau détecteur — `detect_wall_segments`
    marche tel quel en coupe.
  - **Nouveau `lib/dwg_coherence.py`** : `reconcile_plan_section_walls()`
    sans I/O, sans Revit. Conçu pour héberger les futurs reconciles
    cross-vue (plan↔élévation, levels↔coupes, etc.).
- **Tool wrapper unique tier-2** : `dwg_reconcile_plan_section_walls`
  orchestre parse plan + parse coupes + appel module pur. Read-only.
- **Statuses du report** : `ok`, `thickness_mismatch`,
  `no_section_wall_at_x`, `ambiguous_multiple_candidates`. Le tool
  agrège `needs_user_decision` pour qu'un seul check suffise à l'agent.
- **Pas de bulk variant `_many`** pour ce tool : un appel = 1 dossier
  projet = 1 plan, pas une orchestration multi-projets. Cohérent avec
  la coding policy (le bulk s'applique aux ops susceptibles d'être
  appelées en boucle dans une orchestration).

### Phases livrées

**Phase 1 du flow d'import : audit d'intégrité du plan set** (nouveau,
ajouté pendant la session après pivot user).

- **`lib/dwg_coherence.py`** étendu avec 5 helpers purs :
  - `check_source_consistency` : layers AIA/ISO uniformes ?
  - `check_levels_consistency_between_coupes` : jeu de niveaux uniforme
    entre coupes ? Subset → warning ; conflit d'élévation pour même
    nom → error.
  - `check_openings_matching` : openings plan↔coupes matchés ?
  - `check_scale_drift` : drift d'échelle plan↔coupes (≥25% → warning,
    ≥50% → error).
  - `walls_reconciliation_to_check` : convertit le `WallsReconciliation`
    de Phase 2.1 en `IntegrityCheck` (mismatch ≥10cm → error, sinon
    warning).
  - `aggregate_planset_integrity` : combine N `IntegrityCheck` en
    rapport global avec severity max et gate_status
    (`pass`/`needs_user`/`abort`).

- **Tool `check_planset_integrity`** (tier-2, `tools/dwg_import.py`).
  Orchestre : glob `*.dxf` → classify + identify_source par fichier →
  setup checks (manques structurels) → recalcul ou lecture KG des
  section_lines → scale + levels + walls + openings → agrégation. Hard
  gate : `ok=False` si severity=errors.

- **État dégradé géré** (user : « par état dégradé il faut entendre
  tout manque en termes de plans/coupes/élévations ») :
  - 0 plan → error (abort, rien à valider).
  - 0 coupe → warning (`no_section_detected`).
  - 1 coupe seule → warning (`single_section_only`, cohérence
    inter-coupes non vérifiable).
  - 0 élévation → warning (`no_elevation_detected`).
  - Élévations incomplètes (< 4 cardinales) → warning
    (`incomplete_elevations`, expose `directions_present` et
    `directions_missing`).
  - Fichiers `unknown` (ni plan/section/élévation) → **ignorés
    silencieusement** (user : « ignore unknown files »).

- **Routing keywords élargis** (`preprocess.py`) : `audit`, `intégrité`,
  `integrity`, `plan set` (en plus de `recoupement`, `cohérence`,
  `phase 2`).

**Phase 2 étape 1 (recoupement walls plan↔coupes)** — devient une
brique de l'audit ci-dessus, mais reste exposée comme tool indépendant
(`dwg_reconcile_plan_section_walls`) pour un appel ciblé en Phase 2 si
besoin.

**Étape 1.A — `read_section_walls`** (`dwg_section_reader.py`).
Délégation à `dwg_classifier.extract_straight_segments` +
`detect_wall_segments` sur le layer A-WALL, puis filtre paires
verticales par tolérance d'angle (5° à π/2). Sortie triée par
`x_cut_m`. `max_thickness_m=0.60` (vs 0.50 plan) pour absorber les
murs porteurs extérieurs épais et les doubles cloisons visibles en
coupe.

**Étape 1.B — `lib/dwg_coherence.py`** (nouveau module pur). Contient :

- `_segment_intersection_2d` : intersection stricte segment↔segment 2D.
- `WallSectionMatch` + `WallsReconciliation` (dataclasses).
- `reconcile_plan_section_walls(plan_walls, section_lines,
  section_walls_by_coupe, …)` : croise les murs plan avec ceux
  observés en coupe via la convention DXF anchor (`x_cut_attendu = iy`
  pour trait vertical, `ix` pour trait horizontal — cf. mémoire
  `project-dxf-section-anchor-investigation`).

**Étape 1.C — Tool `dwg_reconcile_plan_section_walls`**
(`tools/dwg_import.py`). Tier-2. Lit `DxfImportContext.section_lines`
depuis le KG (ou accepte un override explicite `section_lines=...`).
Parse plan + chaque coupe distincte référencée, délègue au module pur,
sérialise un payload structuré par sévérité (matches_ok tronqué à 200,
mismatches/ambiguous/no_section_wall full).

**Étape 1.D — Routing tier-2 enrichi** (`preprocess.py`). Keywords
ajoutés : `\brecoup(?:e|er|es|…)\b`, `\bcoh[ée]rence\b`, `\bphase\s*2\b`.
« coupe » déjà présent suffisait techniquement mais les nouveaux
keywords solidifient l'intent et préparent le futur
`check_planset_coherence`.

### Tests

**Nouveau `tests/test_dwg_coherence.py`** — 35 tests. Couverture :

- `read_section_walls` (4) : paire verticale, filtrage horizontales,
  ignore non-A-WALL, tri par x_cut.
- `_segment_intersection_2d` (3) : crossing nominal, parallèles, hors
  bornes.
- `reconcile_plan_section_walls` (7) : perfect match, thickness
  mismatch, no_section_wall_at_x, walls plan non croisés, section
  walls unmatched, ambiguous (primary = extension verticale max),
  trait vertical → x_cut = Y world.
- Tool `dwg_reconcile_plan_section_walls` smoke (3) : roundtrip via
  `dispatch_tool_use` avec DXF ezdxf-générés, mismatch →
  `needs_user_decision=True`, erreur remontée quand pas de KG context.
- Helpers audit purs (13) : source_consistency (3), levels_consistency
  (4), scale_drift (3), openings_matching (2), aggregate (1).
- Tool `check_planset_integrity` smoke (5) : clean passes,
  mismatch_severe → abort, dossier vide → error, plan-seul → warnings
  + needs_user, pas-de-plan → abort.

### Validation

**547 tests verts** (512 → 547, +35). Pas de régression sur les tests
existants. Tool registry intact.

### État final & reste à faire

**Audit d'intégrité + Phase 2 étape 1 livrés** :

- `check_planset_integrity` (tier-2) : audit holistique du dossier
  DXF avec hard gate, géré pour les dossiers complets et dégradés.
- `dwg_reconcile_plan_section_walls` (tier-2) : recoupement walls
  exposé comme tool indépendant.

**Nouveau flow d'import attendu (à valider runtime sur P7)** :

1. `check_planset_integrity(directory=...)` ← gate
2. Si `abort` → présenter errors à l'user, stopper.
3. Si `needs_user` → présenter warnings via `ui_confirm_choices`.
4. Si `pass` ou user confirmé → enchaîner Phase 1 traditionnelle
   (`dwg_inspect_sections`, `dwg_find_section_markers`, persistance KG,
   `levels_reconcile_with_dxf`, `views_create_section_many`,
   `views_link_cad_many`, etc.).
5. **Phase 2** seulement après gate vert : détection épaisseurs
   uniques → création types custom → création murs.

**Étapes restantes Phase 2** (cf. mémoire `project-phase2-custom-types`) :
2. Détection épaisseurs uniques observées.
3. Création `WallType` custom `DXF_WALL_<cm>cm`.
4. Création murs (avec `walls_create_many` existant).
5. Ouvertures sill/head.
6. Sols custom + `floors_create_many`.
7. Vue 3D de validation.

**Sous-produit `check_planset_coherence` livré sous le nom
`check_planset_integrity`** — la mémoire `project-planset-coherence-
byproduct` est à actualiser pour refléter que la brique annoncée
« future » a été livrée dans la même session.

### Méta : leçons de session

1. **Réutiliser avant d'écrire** : `dwg_classifier.detect_wall_segments`
   détectait déjà les paires verticales en coupe — pas besoin d'un
   nouveau détecteur. L'audit Phase 1 a évité ~150 lignes de code
   redondant. Toujours faire l'audit avant de coder.

2. **Logique pure vs tool wrapper** : séparer dès le départ permet à
   un futur tool plus large (`check_planset_coherence`) de composer
   sans refactor. Coût marginal aujourd'hui : ~30 lignes
   supplémentaires (module dédié + import). Bénéfice attendu : pas
   de réécriture quand la brique sert ailleurs.

3. **User-decision policy explicite** : `needs_user_decision: bool`
   dans le payload évite à l'agent d'avoir à compter les mismatches
   pour décider quoi faire. Pattern à réutiliser pour les futurs
   tools de check (cf. `all_inferred_confidently` introduit en
   session m pour `dwg_find_section_markers`).

---

## 2026-05-13 (session n) — Phase 1 extensions : élévations + bulks _many + coding policy

### Contexte & objectif

Continuation de session m après validation runtime de Phase 1 sur P7
(projet asymétrique). User a soulevé deux extensions :

1. « je me demande si nous devrions tout de suite compléter la phase
   1 avec les élévations, pour avoir la panoplie complète » — ajouter
   les vues élévation (façades N/S/E/W) au flow Phase 1.
2. « est-ce que ça pourrait être moins coûteux en termes de tokens ?
   dxf_context_register_many_linked_view et autres améliorations ? »
   — réduire les 16+ appels lockstep observés en runtime P7 via bulk
   variants `_many`.

Plus une coding policy formalisée en réaction à la 2e remarque.

### Phase 1 — Étape 7 (bonus) : élévations livrées

**Commit `8281606`** (`ADD Phase 1 élévations`).

- `classify_dxf` étendu avec arg optionnel `file_name` :
  - Détecte `kind='elevation'` quand filename contient « élévation » /
    « elevation » + signature A-FLOR-LEVL (sinon ces fichiers seraient
    classés section, signature layers identique).
  - Parse `direction` ∈ {Est, Nord, Sud, Ouest} avec ORDRE IMPORTANT :
    Ouest avant Est car « est » substring « ouest ».
  - Détecte aussi `kind='plan'` via filename (« Plan », « Niveau »)
    pour les plans sans `A-AREA-IDEN` (cas P7 sans Pièce labels).

- `dwg_inspect_sections` étendu pour traiter `kind='elevation'` :
  extraction levels + openings (même logique que section), expose
  `direction` dans le rapport.

- **Nouveau tool `catalog_list_elevation_views`** tier-1 :
  - `FilteredElementCollector(doc).OfClass(View).OfType(Elevation)`.
  - Priorité au **nom de la vue** (« Élévation Est », « Élévation
    Nord », etc.) pour le matching de direction — plus fiable que
    `view.ViewDirection` dont la convention de signe est ambiguë.
  - Fallback : dot product avec les 4 vecteurs cardinaux.

- **Convention élévation DXF validée P7** : A-WALL bbox de chaque
  élévation matche EXACTEMENT le plan A-WALL (X ou Y, avec sign flip
  selon viewer direction). La logique existante `views_link_cad`
  marche pour ViewElevation (hérite de ViewSection).

### Bug fix : `SectionOpening` non sérialisable (commit `1d9ae5b`)

User runtime P7 : « erreur de sérialisation JSON interne
(SectionOpening non sérialisable) ». Cause : avec 2 plans (N0 + N1),
seul le premier voyait son `_openings_internal` poppé avant return.
Les autres plans gardaient des SectionOpening objects → crash JSON.

Fix : itérer TOUS les records après matching et pop `_openings_internal`
inconditionnellement.

### Bug fix : inversion Est ↔ Ouest élévations (commit `60bcc52`)

User runtime P7 : « inversion Est - Ouest ». Cause : `direction_map`
basée sur `view.ViewDirection` (doc Revit ambiguë : look-direction vs
toward-viewer). Fix : prioriser le NAME de la vue (« Élévation Est »
contient explicitement la direction), vecteur en fallback.

### Bulk variants _many (commit `bfc8c80`)

User mesure du runtime P7 : « 16 appels lockstep ». Concrètement :
`views_link_cad × 8` + `dxf_context_register_linked_view × 8` =
2 × overhead × 8 = ~1600 tokens + 8 round-trips API séquentiels.

**4 nouveaux tools bulk** livrés :

1. **`views_link_cad_many(links, placement?, color_mode?, restore_pinned?)`**.
   Helper privé `_link_cad_to_view` extrait — réutilisé par le tool
   unitaire (qui wrap avec transaction) ET le bulk (qui wrap N appels
   dans une seule transaction). Validation pre-loop : tout est validé
   avant le moindre commit.

2. **`views_create_section_many(items, bottom_elev?, top_elev?, ...)`**.
   Section ViewFamilyType cherchée 1× au lieu de N. Validation noms
   uniques intra-batch.

3. **`dxf_context_register_linked_view_many(entries)`** : N enregistre-
   ments en 1 modification du DxfImportContext (1 `modify_node` vs N).

4. **`dxf_context_register_section_line_many(section_lines)`** : pareil
   pour les traits de coupe.

### Coding policy formalisée (commit `3c018af`)

User : « c'est à placer dans notre coding policy : en règle générale,
si une fonction est susceptible d'être sollicitée en bulk dans une
orchestration, son équivalent bulk doit être créé ».

Règle formalisée à 3 niveaux :
- **`CLAUDE.md`** nouvelle section « Coding policies » avec pattern
  d'impl (helper privé + unitaire/bulk wrappers + Tx unique).
- **Mémoire feedback** `feedback-bulk-tool-variant-policy` : règle
  complète + liste des bulks existants + manquants potentiels à
  examiner (`rooms_create_many`,
  `levels_create_floor_plan_many`, etc.).
- **MEMORY.md** index mis à jour.

Cette policy s'applique en amont des futures additions de tools (pas
juste réactive).

### Validation runtime P7 (3e + 4e itérations)

3e (avant bulks) : « ça me semble correct » — Phase 1 fonctionnelle
avec élévations.

4e (avec bulks) — comparaison mêmes import P7 :

| Métrique | Avant bulks | Après bulks | Δ |
|---|---|---|---|
| in tokens | 51 216 | 26 600 | **−48%** |
| out tokens | 5 462 | 4 193 | −23% |
| Tool calls totaux | 28 | 13 | −54% |

Tool log final agent (13 appels seulement) :
```
dwg_inspect_sections, levels_reconcile_with_dxf,
dwg_find_section_markers, catalog_list_levels,
catalog_list_elevation_views, dxf_assign_coupes_to_traits,
ui_confirm_yes_no, dxf_context_register_inspection,
views_create_section_many, dxf_context_register_section_line_many,
views_link_cad_many, dxf_context_register_linked_view_many,
views_open_3d
```

### Validation cumulée

**512 tests verts** (session m fin : 503 → +9 pour bulks + élévations).

Suite couvre :
- 9 nouveaux : 4 views (link_cad_many, create_section_many) + 4
  dxf_context (register_linked_view_many, register_section_line_many) +
  classify_dxf élévation parametrized.

### État final & reste à faire

**Phase 1 PRODUCTION-READY + token-efficient** : 8 outils livrés
(coupes + plans + élévations + vue 3D auto) + 4 bulks. Workflow runtime
stable et économe (~26K tokens par import).

**Phase 2 spec figé** (cf. mémoire `project-phase2-custom-types`) :
création de types custom DXF_WALL_xxcm / DXF_FLOOR_xxcm sans matching
des types existants. 7 micro-étapes prévues. Pas encore codé.

**Dettes ouvertes** :
- Orientation fenêtre dans le mur (task #10) — Phase 2 sujet.
- Convention DXF Projet4 (offset Y -8000mm vs P7 zero-offset) — pas
  ré-investigué, V1+.
- Bulks manquants potentiels (rooms_create_many, etc.) — examiner si
  besoin runtime.

### Méta : leçons de session

1. **Asymetric test data > heuristics** : P7 a tranché en 5 minutes
   les ambiguïtés de Projet4 (DXF anchor) et de elevation direction
   conventions. À retenir : pour calibrer une convention API
   sous-documentée, demander à l'user un cas asymétrique.

2. **Le bulk variant doit être proactif** : la coding policy en
   `CLAUDE.md` impose désormais le `_many` dès la conception. Économie
   moyenne ~50% des tokens d'orchestration.

3. **NAME > vecteur pour discrimination** : pour `catalog_list_elevation
   _views`, le nom de la vue était plus fiable que `ViewDirection`
   dont la convention de signe est ambiguë. Quand un attribut a
   plusieurs interprétations possibles, préférer la donnée la plus
   contrainte (= name explicit).

4. **Cleanup data leakage** : le bug `_openings_internal` non poppé
   sur les N-ème plans rappelle que tout state interne doit être
   nettoyé inconditionnellement (pas seulement sur le chemin
   « heureux »).

---

## 2026-05-13 (session m) — UC1 Phase 1 import projet : 6 étapes livrées + 11 fixes runtime + Floor type + UI dialogs

### Contexte & objectif

Session marathon issue d'un cadrage user explicite : « ce process
devrait être décomposé de manière plus détaillée avant de faire des
tests ». L'import projet (« importe ce projet <dossier> ») est
redécoupé en **6 sous-étapes auditables avec gate user entre chaque** :

1. Identifier coupes/plans
2. Situer les traits de coupe dans le plan
3. Vérifier l'échelle plan ↔ coupes
4. Identifier la nomenclature des layers
5. Reconcile niveaux DXF ↔ Revit
6. Linker les DXF dans Revit (références visuelles)

Strict : **STOP après Phase 1 (setup uniquement), pas de création de
murs/ouvertures/sols** — c'est la Phase 2.

### Phases livrées (Phase 1 complète à 6/6)

**Étape 1 — `dwg_inspect_sections` + `DxfImportContext`** (commits
`39d2f57`, `9824df4`, `8d5f418`). Module `lib/dwg_section_reader.py` :
classify_dxf, parse_block_id (regex tolérant `V1`/`V2` après bug
runtime), parse_block_dimensions, read_levels avec 4 sources fallback,
read_section_openings, match_openings par block_id. Tool tier-2
`dwg_inspect_sections(directory=...)` avec convention « importe ce
projet C:/... ». Nœud KG `DxfImportContext` singleton-ish + 4 tools
`dxf_context_*` pour persistance entre tours.

**Étape 2 — `dwg_find_section_markers`** (commits `1751d06`, `a5652ae`).
Détection sur layer `G-ANNO-SYMB` (Revit AIA export). LINEs + INSERTs
aux endpoints, discrimination « Coupe ... » vs « Elévation ... » via
block name. Amélioration : `inferred_view_dir` calculé depuis la
rotation du bloc marqueur (default direction +Y, CCW rotation). Le
tool expose `all_inferred_confidently` pour que l'agent procède sans
confirmation systématique.

**Étape 3 — `dwg_verify_section_scale`** (commit `1751d06`). Sanity
check drift entre ||p2-p1|| du trait et X-extent A-WALL de la coupe.
Bug user : « la grande coupe à la place de la petite ». Fix dédié
commit `33a0757` : **nouveau tool `dxf_assign_coupes_to_traits`** qui
brute-force toutes les permutations N coupes × N traits et retourne
l'assignment optimal par minimum drift total. Sur P7 : drift 9.89m
(optimal) vs 14.60m (swap), 33% mieux.

**Étape 4 — `dwg_identify_source`** (commit `aea8fb0`). Convention
AIA / ISO / other. Heuristique sur ratio de layers matching. P7 et
Projet4 sont AIA confidence 1.0.

**Étape 5 — `levels_reconcile_with_dxf`** (commit `f18cf72`). Diff
3 passes (match exact, name only mismatch, elev only mismatch).
Output `summary_for_dialog` formaté pour dialog groupé. Règle user
explicite : niveaux exigent TOUJOURS validation, mais GROUPÉE en
1 dialog (cascade sur les hôtes = raison de garder l'user dans la
boucle même quand l'inférence est sûre).

**Étape 6 — `views_create_section` + `views_link_cad` +
`views_open_3d`** (commits `ec94977`, `c2f0cd4`). Création ViewSection
Revit avec math BBox/Transform isolée dans `section_view_geom.py`
(testable hors-Revit, 13 tests pure-Python). Link CAD avec
`ImportPlacement.Origin` + translation post-link pour aligner sur
le cut plane. Fin de Phase 1 : `views_open_3d` active la vue 3D
par défaut pour validation visuelle immédiate.

**`ui_confirm_choices` / `ui_confirm_yes_no`** (commit `f68ca2e`).
TaskDialog Revit modale, option A retenue par user (vs vraie
non-bloquante dock pane = trop d'effort V0). Convention : 1 clic
au lieu de tapoter une réponse texte. **Règle UX importante** (feedback
memory `feedback-user-confirmation-when-doubt`) : confirmer
uniquement en cas de doute, pas systématiquement. Exception
niveaux.

### Convention DXF (0,0) Revit section export — résolue via P7

User a fourni un mini-projet **asymétrique** P7 pour dériver la
convention sans bruit de symétrie. Preuve flagrante : A-WALL bbox du
plan = X[-15589.6, +1610.4], Y[-1274.5, +8925.5]. A-WALL bbox de
Coupe 1 (verticale) X = [-1274.5, +8925.5] = **EXACT match** plan Y.
A-WALL bbox de Coupe 2 (horizontale) X = [-15589.6, +1610.4] =
**EXACT match** plan X. Aucun décalage le long du cut.

**Convention finale** (commit `c40a308`) :
- Coupe verticale (cut along world Y) : DXF X ↔ world Y identité,
  DXF (0,0) → world `(X_cut, 0, 0)`
- Coupe horizontale (cut along world X) : DXF X ↔ world X identité,
  DXF (0,0) → world `(0, Y_cut, 0)`

Implémentation : `views_link_cad` détecte vertical/horizontal via
`BasisX.X` (≈ 0 pour vertical, > 0.5 pour horizontal), translate
en conséquence. **Pas** `view.Origin` (= midpoint du trait) qui
incluait un offset incorrect le long de la section direction.

Cf. mémoire `project-dxf-section-anchor-investigation` mise à jour.

### Floor / Sol type (UC2 V0)

Commit `d5ecc30`. User a demandé en milieu de session : « peux-tu
ajouter les ops Sol/dalle, ils seront testés avec les premiers
imports de coupes ». Livré complet :

- Schema KG `Floor` + `FloorType` (project_kg.py)
- Module `lib/tools/floors.py` : `floors_create`, `floors_create_many`,
  `floors_delete`, `floors_delete_many`. Validation boundary
  (≥ 3 sommets, pas de doublons adjacents, dédup trailing). Aire
  via shoelace côté KG, lue depuis `HOST_AREA_COMPUTED` côté Revit.
- Catalog : `catalog_list_floors` + `catalog_list_floor_types`.
- Revit primitives + kg_sync (scan FloorType + Floor dans
  full_rescan).
- Preprocess auto-scan : `sols?/dalles?/planchers?/slabs?` →
  `catalog_list_floors`.
- 25 tests.

### 11 fixes runtime itératifs (debug Phase 1)

User a fait 4-5 tests runtime successifs avec retours détaillés (logs
+ screenshots 3D). Chaque retour a déclenché un fix précis :

1. **Scope Phase 1** (`40e4c15`) — agent créait 66 murs au lieu de
   s'arrêter. Règle explicite system prompt « STOP après linkage ».
2. **ImportColorMode** (`40e4c15`) — `PreserveColorMode` n'existe
   pas en Revit 2025. Mapping `by_layer` redirigé vers `Preserved`.
3. **BBox center vs bottom** (`40e4c15`, `305f27f`) — itéré 2 fois.
   Origin.Z = bottom_elev (pas center) pour aligner DXF y=0 sur
   world Z=0 (= Niveau 0).
4. **clr.Reference** (`e233a50`) — PythonNet 3.x ship dans pyRevit
   Master a dropped `clr.Reference[Type]()`. Convention moderne :
   tuple-return pour les `out` params .NET. `result = doc.Link(...)`
   puis unpacking `ok, out_id = result`.
5. **`floor_plan_view_revit_id`** (`305f27f`) — agent n'avait pas
   l'id de la vue plan pour linker le plan DXF. Exposé via
   `catalog_list_levels` en runtime Revit.
6. **BasisZ + Min.Z/Max.Z** (`17965e8`, `80ea0db`) — itéré 2 fois.
   Convention finale : BasisZ = +look (sens du regard), Min.Z=0
   (cut plane), Max.Z=+far_clip (back of view). Avant : BasisZ=-look
   + Max.Z=0 ne fonctionnait pas comme attendu.
7. **MoveElement sur élément verrouillé** (`345a54b`) — Revit auto-
   épingle les links avec OrientToView=True. Fix : unpin avant
   MoveElement.
8. **restore_pinned** (`8721430`) — user a demandé de re-pingler après
   l'alignement (comportement Revit standard).
9. **Coupe-swap** (`33a0757`) — agent matchait par ordre des fichiers
   (alphabétique) au lieu de l'ordre des markers (par longueur).
   Tool `dxf_assign_coupes_to_traits` brute-force la bonne
   permutation par min drift.
10. **Translation par convention vraie** (`c40a308`) — `view.Origin`
    incluait un offset midpoint incorrect le long de la section.
    Convention P7-derived appliquée.
11. **`views_open_3d`** (`c2f0cd4`) — auto-activation de la vue 3D
    en fin de Phase 1 pour validation visuelle immédiate.

### Validation finale (P7 runtime test 5e itération)

```
Tools utilisés : dwg_inspect_sections, levels_reconcile_with_dxf,
dwg_find_section_markers, catalog_list_levels, ui_confirm_yes_no,
dxf_assign_coupes_to_traits, dxf_context_register_inspection,
views_create_section × 2, dxf_context_register_section_line × 2,
views_link_cad × 4, dxf_context_register_linked_view × 4,
views_open_3d
```

User confirme : « alignements corrects :-) ».

- 4 DXF linkés (2 plans + 2 coupes) aux bons positions
- 2 ViewSections créées, niveaux 0/3/6m alignés
- Coupe 1 (DXF petit) → trait vertical 14.84m ✓ (anti-swap)
- Coupe 2 (DXF grand) → trait horizontal 22.45m ✓
- Vue 3D auto-active en fin de phase

### Mémoires écrites

- `feedback-user-confirmation-when-doubt` : confirmation uniquement
  en cas de doute, exception niveaux (validation toujours mais
  groupée).
- `project-uc1-coupes-priority` : géo-ref AVANT extraction hauteurs.
- `project-dxf-layer-conventions` : AIA standard, à étendre pour
  ArchiCAD/ISO/BS1192.
- `project-dxf-section-anchor-investigation` : convention DXF (0,0)
  Revit section RÉSOLUE (initialement « en cours »).

### Validation cumulée

**500 tests verts** (353 en début de session → +147). Suite
couvre :
- 32 tests `test_dwg_sections` (section_reader + find_markers +
  verify_scale + identify_source + reconcile + projet4
  integration)
- 13 tests `test_section_view_geom` (math BBox pure)
- 12 tests `test_dxf_context` (persistance)
- 11 tests `test_views` (views_create_section + link_cad +
  register_linked_view + open_3d)
- 25 tests `test_floors` (Floor/Sol complet)
- 11 tests `test_ui` (confirm_choices, confirm_yes_no)
- + tests existants étendus pour nouveaux tools

### État final & reste à faire

**Phase 1 import projet : PRODUCTION-READY** sur projets AIA. Workflow
runtime stable, validation visuelle via vue 3D auto-active.

**Phase 2 ouverte** : « crée les murs depuis le plan ». L'agent
réutilisera `dwg_import_walls` (avec garde-fou anti-section). Probable
chantier suivant : enrichir les ouvertures avec hauteurs sill/head
extraites des coupes (via `DxfImportContext.section_lines` persisté).

**Dettes ouvertes (notées en cours de session) :**
- Orientation fenêtre dans le mur (task #10) — Phase 2 sujet.
- Marqueurs d'élévation standalone (task — V0 algo requires LINE,
  V1 = pass standalone INSERTs).
- Convention DXF Projet4 (offset Y -8000mm) — pas la même que P7.
  Hypothèse : version d'export ou option Revit différente. À
  ré-investiguer si on retombe dessus.
- Auto-création niveaux depuis coupes (task #16) — `levels_create_many`
  existe, manque la guidance prompt.

### Méta : leçons de session

1. **Convention Revit empirique > docs** : 3 itérations BasisZ
   nécessaires car la doc Revit API était ambiguë. Le test asymétrique
   (P7) a tranché en 5 minutes ce qui résistait depuis 1h. Toujours
   préférer un cas asymétrique pour dériver une convention.
2. **Gate user entre étapes Phase 1** marche en pratique — l'agent
   s'arrête, l'user voit, valide. Sans ce gate, l'agent aurait commit
   les murs dès le 1er tour (cf. test session avant fix `40e4c15`).
3. **Tool design > prompt engineering** : `dxf_assign_coupes_to_traits`
   livré comme tool plutôt qu'instruction prompt = comportement
   déterministe et auditable. Le prompt seul ne suffisait pas pour
   le matching optimal.
4. **`run live test` côté Revit > tests synthétiques** pour les
   conventions API peu documentées. Les tests pure-Python n'auraient
   jamais découvert le bug `clr.Reference` ou le pinning auto de
   OrientToView.

---

## 2026-05-13 (session l) — UC1 Phase 4 coupes : inventaire + dwg_section_reader + tool dwg_inspect_sections

### Contexte & objectif

Après validation runtime session k (le fix pair detection corrige
visuellement la cloison fragmentée — 2 murs parallèles résiduels en
runtime mais acceptables en manuel), démarrage du chapitre **coupes**
(note d'intention 2026-05-12). User a réordonné les priorités en cours
de session : **« en premier lieu il faut repérer les traits de coupe
en plan, de manière à interpoler les dessins correctement »** → la
géo-ref doit précéder l'extraction des hauteurs.

### Inventaire DXF Projet4

3 fichiers : `Projet4 - Plan d'étage - Niveau 0.dxf`,
`Projet4 - Coupe - Coupe 1.dxf`, `Projet4 - Coupe - Coupe 2.dxf`.
Chaque vue dans son propre fichier (→ phase 0 « segmentation » de la
note d'intention devient triviale).

Conventions AIA respectées :
- `A-FLOR-LEVL` (coupes) : 3 LIGNES horizontales à Y=0/3000/6000 (mm)
  + 6 MTEXT par paires (`Niveau 0` / `0`, `Niveau 1` / `3.00`,
  `Niveau 2` / `6.00`) + 3 INSERT `Niveau - Marqueur de niveau`.
- `A-GLAZ` : INSERT de blocs fenêtre. Block names dont l'**ID Revit
  numérique est partagé entre plan et coupes** :
  - Plan : `... -255828-Niveau 0`
  - Coupe 1 : `... -255828-Coupe 1`
  - Le `255828` matche → pivot de matching coupe ↔ plan sans
    géométrie supplémentaire.
- `A-AREA-IDEN` (plan uniquement) : MTEXT labels pièces.
- Dimensions encodées dans le nom de bloc : `2_00 m x 1_40 m`
  (underscore décimal = échappement du `.` Windows).

**Aucun trait de coupe marqué dans le plan** : pas de layer `A-SECT`,
pas de bloc INSERT « symbole de coupe », pas de MTEXT « A-A » / « B-B ».
→ Géo-ref par pointage user obligatoire (auto-détection par feature
matching reste possible en V1 si DXF moins bien fait).

### Décisions

1. **Phase A (was 3) géo-ref FIRST**, par pointage utilisateur. La
   détection auto par matching INSERT n'est pas le mécanisme principal
   mais pourra servir de pré-remplissage en V1.

2. **Premier livrable read-only** : tool `dwg_inspect_sections` qui
   parse N fichiers et sort un rapport JSON. Pas d'écriture Revit. Le
   LLM ou l'user décide ensuite des actions à prendre.

3. **Pas de nouveau module ezdxf** : `dwg_section_reader` consomme les
   `DwgEntity` retournés par `dwg_reader.parse()` (qui supporte déjà
   INSERT, MTEXT). Cohérent avec le pattern dwg_classifier.

4. **Fixtures de test** : synthétiques (DXF générés en mémoire via
   ezdxf) + integration optionnelle sur les 3 fichiers Projet4 (skip
   propre si absents).

### Phases livrées

**Phase 1** — `lib/dwg_section_reader.py` (nouveau).
- `classify_dxf(layers_meta) -> ("plan"|"section"|"unknown", evidence)`.
- `parse_block_id(name)` : regex `-(\d{4,})-(?:Niveau|Coupe|...)` →
  ID numérique.
- `parse_block_dimensions(name)` : regex `(\d+)_(\d+) m x (\d+)_(\d+) m`.
- `read_levels(entities) -> List[Level]` : associe LIGNES horizontales
  + MTEXT proches (vertical tol 2m, horizontal tol 1m), distingue nom
  vs valeur par contenu. Fallback chain :
  - mtext_label+value (cas normal)
  - mtext_value_only
  - mtext_label_only_inferred_elevation (élév déduite de Y_ligne)
  - line_only_inferred (élév = Y_ligne, nom = `Niveau N`)
- `read_section_openings(entities) -> List[SectionOpening]` : INSERTs
  sur `A-GLAZ` + parsing block_id + dimensions.
- `match_openings(plan, section) -> (matches, unmatched_sec, unmatched_plan)`
  par block_id partagé.

**Phase 2** — Tool `dwg_inspect_sections` dans `lib/tools/dwg_import.py`.
- Tier 2. Read-only. Accepte une liste de file_paths.
- Pour chaque fichier : classify + extract (selon kind).
- Calcule section_to_plan_matches : pour chaque coupe, donne match_count,
  unmatched_*, distinct_block_ids.
- Docstring convention (Concepts/Phrases/Similar) renseignée pour
  routing tier-2 + future intégration au KG logiciel.

**Phase 3** — Tests `tests/test_dwg_sections.py` (nouveau).
- 15 tests synthétiques (parse_block_id, parse_block_dimensions,
  classify_dxf, read_levels avec et sans labels, read_section_openings,
  match_openings).
- 6 tests d'intégration sur Projet4 (skip propre via
  `pytest.mark.skipif`). Asserts précis : 22 openings coupe 1, 20
  openings plan, 3 niveaux à 0/3/6m, matching complet.

### Validation

- `pytest -q` : **374 verts** (353 → +21). Aucune régression.
- Test live direct via registry : tool registered correctly, output sur
  Projet4 :
  - Coupe 1 : 22 openings (2× Sud 255828, 2× Nord 255829, 18× Est
    255830 sur 2 niveaux), 3 niveaux à 0/3/6m, **22/22 matched** au
    plan, unmatched_plan=9 (les 9 Ouest absents de Coupe 1).
  - Coupe 2 : 6 openings (2× Nord, 2× Est, 2× Ouest), **6/6 matched**,
    unmatched_plan=1 (le Sud absent de Coupe 2).
  - Plan : 20 openings (1+1+9+9 par façade), tous avec block_id reconnu.

### Dettes notées en cours de session (user)

1. **Orientation fenêtre dans le mur** : lors de la création de
   fenêtres (`windows_create_many` ou copie), la rotation/face de la
   fenêtre dans le mur n'est pas prise en compte. À traiter : capter
   l'orientation depuis le bloc DXF (rotation = façade) ou depuis le
   mur hôte (face inside/outside).

2. **Type sol/dalle absent** : pas de Floor dans le KG, pas de tools
   `floors_*`. Nouveau chantier proposé : `lib/tools/floors.py` avec
   `floors_create`, `floors_create_many`, `floors_delete`,
   `query.floors_list_by_level`. KG node `Floor` avec
   `{level_ref, floor_type_ref, boundary_polygon, thickness_m}`.

### Reste à faire pour le chantier coupes (suite)

- **Phase B** : pointage utilisateur du trait de coupe — soit via
  dialog Revit (2 points sur le plan + direction de vue), soit en
  conversationnel via le LLM (« la coupe 1 est prise selon une
  horizontale à Y=0, regardant l'Est »). Stocker dans le KG (nouveau
  node `SectionAxis`?). Cf. dette `dwg_section_reader` interpolation.
- **Phase C** : extraire sill/head des INSERTs en coupe — déjà
  largement résolu par `parse_block_dimensions` (height_m = 1.40 par
  ex) mais à enrichir par l'Y d'insertion (= sill depuis le niveau).
- **Phase D** : orchestrateur `dwg_import_full` qui combine plan walls
  + niveaux + ouvertures avec hauteurs correctes.

---

## 2026-05-13 (session k) — Dette pair detection réglée : endpoint adjacency + length ratio

### Contexte & objectif

Suite directe session j. Attaque de la dette pair detection
identifiée : faux pair entre cloison simple-trait fragmentée et mur
isolé parallèle (cas Projet4 — fragment 4.81m face à mur 19.80m
parallèle à 0.20m).

### Décisions

1. **Contrainte topologique : adjacence d'endpoints.** Une vraie paire
   de faces de mur a au moins 1 endpoint joint perpendiculairement
   par un épaulement (coin). Une « paire » fortuite entre 2 cloisons
   distinctes a tous ses endpoints éloignés. Critère :
   `_min_endpoint_distance(s_i, s_j) <= max_thickness_m × 2`.

2. **Insuffisant à lui seul** sur le cas Projet4 : les 2 segments
   partagent par coïncidence un endpoint au coin du bâtiment (y=9.90).
   Distance 0.20m → passe le critère endpoint.

3. **Ajout critère ratio de longueur.** Une vraie paire de faces a des
   longueurs similaires (à fragmentation près). Critère :
   `max(L_i, L_j) <= max_length_ratio × min(L_i, L_j)`, défaut
   `max_length_ratio=4`.

   Sur Projet4 : 19.80 / 4.81 = 4.11 > 4 → faux pair rejeté. ✓
   Sur façades fragmentées : segments de longueur similaire (ratio ≈ 1)
   → match. ✓
   Sur T-jonction limite (face courte 5m + face longue 10m, ratio 2)
   → match. ✓
   Sur faux pair Projet4-style (ratio > 4) → rejet.

4. **Flags configurables** : `require_endpoint_adjacency=True` et
   `max_length_ratio=4.0` sur `detect_wall_segments`. Désactivables
   pour les cas atypiques où l'utilisateur sait que sa géométrie
   sort des conventions.

### Résultat sur Projet4

Avant fix (session j v2) :
- 29 pairs (dont 1 faux à centerline x=3.80, partie haute de la
  cloison).
- 3 centerlines (dont 1 vrai résidu de cloison 7.49m + 2 chambranles
  faux positifs).
- **Visuellement** : la cloison apparaissait fragmentée en 2 walls
  décalés de 10cm en x (3.80 vs 3.70).

Après fix (session k) :
- **28 pairs** (1 faux pair éliminé).
- **6 centerlines** :
  - **3 segments alignés à x=3.70** : 4.81 + 7.80 + 6.79 = 19.40m, la
    vraie cloison interne en 3 morceaux séparés par des portes
    (gaps > 0.20m donc pas fusionnés). **Plus de décalage 10cm.** ✓
  - **1 mur séparé à x=3.90** (19.80m) : ancien membre du faux pair,
    maintenant traité comme centerline isolé.
  - 2 trumeaux à x=10 et x=0 (faux positifs persistants comme avant
    — chambranles biais).

Total 34 walls (vs 32 avant). Le mur à x=3.90 est désormais correctement
traité comme un mur isolé (probablement une face d'un mur structurel
sans paire dans le DXF).

### Tests

2 nouveaux tests dans `tests/test_dwg.py` :
- **`test_pair_detection_rejects_endpoint_distant_false_pair`** : exact
  reproduction du cas Projet4 (4.81m face à 19.80m, ratio 4.11) →
  refus, 2 segments orphelins.
- **`test_pair_detection_accepts_similar_length_aligned_faces`** :
  vraie paire (2 segments de 5m, perp 0.20m, endpoints alignés) →
  match.

### Validation

- `pytest -q` : **353 verts** (351 → +2). Aucune régression sur la
  suite synthétique (fixtures `_make_rectangle_room_dxf` avec paires
  de longueurs identiques → ratio 1 → pass).
- Runtime à valider à la prochaine session côté Revit (re-import
  Projet4 DXF pour confirmer visuellement).

### Acquis session k

- Critère endpoint adjacency ✓
- Critère length ratio ✓
- Test ciblé du cas Projet4 ✓
- **Dette pair detection greedy réglée** pour les cas typiques ✓

### Dettes restantes

- **Chambranles biais aux fenêtres** (faux positifs 1.20m sur façades) :
  pas fixé. Heuristique angle-strict (tol 2°) ne les distingue pas
  des cloisons légères. Workaround user : delete_many.
- Plus large : reste les dettes héritées (routing tier-2 propre,
  get_element_or_raise étendu, drift utilisateur, boundary_walls,
  connects_at, catalog_list_views).

---

## 2026-05-13 (session j) — Runtime UC1 DWG suite : centerline subtraction, 529 retry, dette pair detection

### Contexte & objectif

Suite directe de session i. User a continué le test 6 sur le DXF
Projet4. 3 issues remontées en runtime, toutes traitées (2 fixes
livrés, 1 dette identifiée et reportée).

### Issue 1 : centerline doublons par chevauchement partiel

Avec la version session i v1 (filtre anti-shadow tout-ou-rien à
`overlap_threshold=0.80`), la cloison interne se retrouvait en
**doublon** : un pair partiel détecté en haut, un centerline complet
en bas, qui chevauchent à 59% (sous le seuil 0.80 donc pas filtré).

**Fix** : remplacer le filtre tout-ou-rien par une **soustraction
d'intervalles 1D**.

`_subtract_pair_shadows(candidate, pair_walls, ...)` :
1. Pour chaque pair quasi-parallèle proche en perp (< 0.30m),
   projeter ses endpoints sur la droite portante de la candidate.
2. Collecter les intervalles d'ombre clampés à `[0, L]` (pour la
   soustraction effective) + les intervalles non-clampés (pour
   calculer l'enveloppe totale).
3. Soustraire les ombres mergées du candidate → liste de résidus.
4. **Filtre enveloppe** : un résidu entièrement dans l'enveloppe
   `[pair_t_min, pair_t_max]` est probablement dans un « trou de
   fenêtre » (entre 2 pairs adjacents d'une façade fragmentée)
   → filtré.

Résultat sur Projet4 :
- Avant session i v1 : 5 centerlines dont 4 doublons sur façades.
- Session i v2 (subtraction + enveloppe) : 3 centerlines dont
  1 vraie cloison résiduelle de 7.49m et 2 chambranles biais
  faux positifs.

### Issue 2 : faux pair detection (DETTE — non corrigée cette session)

User a remarqué : « la paroi centrale est fragmentée en deux parties
bout-à-bout décalées de 10cm » côté Revit.

**Diagnostic** (via debug CLI direct sur le DXF) :
- La cloison interne réelle est dessinée **en simple-trait** à
  x=3.70 (5 segments fragmentés par 4 ouvertures = portes).
- Un autre segment isolé existe à x=3.90 (long de 19.80m, probable
  face d'un mur structurel adjacent — pas la paire de la cloison).
- **Pair detection greedy** apparie un fragment x=3.70 avec le segment
  x=3.90 (distance 0.20m, parallèles, overlap suffisant) → faux pair
  à centerline x=3.80, thickness 0.20m. C'est une **mauvaise
  interprétation** : ces 2 segments représentent 2 cloisons distinctes.
- Le résidu de la vraie cloison (fragments non « volés » par la fausse
  paire) passe en centerline à x=3.70, thickness 0.10m.
- Côté Revit : 2 walls bout-à-bout décalés de 10cm — visuellement
  une cloison fragmentée.

**Cause racine** : `detect_wall_segments` est greedy first-match.
Pour chaque segment i, il prend le premier j qui passe les filtres
(angle + distance + overlap). N'évalue pas si i pourrait avoir une
**meilleure** paire (perp distance plus courte, overlap supérieur),
ni si j est isolé (= la « paire » est en fait coïncidence
géométrique).

**Fix correct** : refonte de l'algo pair detection pour préférer le
« meilleur voisin perpendiculaire » plutôt que le premier candidat.
Heuristique possible : pour chaque segment, parcourir tous les
candidats matchant les 3 filtres, prendre celui avec perp distance
minimale ET overlap maximal. Vérifier aussi que le candidat retenu
n'a pas lui-même un meilleur match ailleurs (symétrie).

**Effort estimé** : ~1 jour. Reporté pour ne pas exploser cette
session. Documenté comme dette UC1 V0 → V1.

**Workaround user immédiat** : identifier visuellement les faux
pairs dans Revit + utiliser `walls_delete_many` pour les supprimer.

### Issue 3 : `overloaded_error` 529 sur demande de suppression en masse

User a observé l'erreur Anthropic 529 (« Site is overloaded »)
pendant une demande de suppression de plusieurs centaines d'éléments.
Le SDK Anthropic retry par défaut 2 fois avec backoff exponentiel
2+4 = 6s cumulés — épuisé rapidement sur un pic prolongé.

**Fix en deux couches** :

1. **SDK** : `max_retries=2 → 6` sur le constructeur `anthropic.Anthropic`.
   Couvre 2+4+8+16+32 = ~62s de backoff cumulé.

2. **Outer retry** dans `_create_with_outer_retry` :
   - Wrappe `client.messages.create()`.
   - Catch transient errors (408, 429, 5xx, 529, APIConnectionError,
     APITimeoutError).
   - 4 retries × 30s sleep = ~2 min supplémentaires.
   - Total max absorbable : ~3,5 min de saturation Anthropic.

3. **UX dialog dédié** dans `prompt.pushbutton` :
   - Catch `status_code == 529 OR error_type == "overloaded_error"`.
   - Dialog `« Anthropic saturé — réessayer »` avec lien
     status.anthropic.com, au lieu du traceback brut.
   - Précise que l'historique n'est PAS modifié (le tour n'a pas eu
     lieu) → safe de relancer.

### Validation

- `pytest -q` : **351 verts**. Aucune régression.
- Tests live runtime à la prochaine session si pic Anthropic se
  reproduit.

### Acquis session j

- Subtraction d'intervalles pour centerline filter ✓
- Outer retry + UX 529-spécifique ✓
- SDK `max_retries=6` ✓
- **Dette pair detection greedy** documentée ✓

### Dettes ouvertes (héritage)

- **Pair detection greedy → faux positifs** sur DXF avec cloisons
  simple-trait proches de murs parallèles isolés (cette session).
  Refonte ~1 j. Workaround user : delete_many.
- **Routing tier-2 propre** (session i) — `preprocess.infer_tier_max`
  est minimal, à factoriser quand UC8 / UC6 arrivent.
- **`get_element_or_raise`** non étendu aux walls/columns
  (session g).
- **Drift utilisateur hors pipeline** (events `DocumentChanged`).
- **`boundary_walls`** Rooms reporté V1 compliance.
- **`connects_at`** peuplé au rescan + `catalog_list_views`
  (préreqs auto-cotation + UC6).

---

## 2026-05-12 — Note d'intention : UC1 Phase 4 — intégration des coupes DXF

Conversation exploratoire (session i, post-validation runtime).
**Pas de code livré**. Capture la demande utilisateur de combiner
plan + coupes du même DXF pour reconstituer le modèle 3D avec les
bonnes hauteurs (allèges, linteaux, niveaux).

### Demande utilisateur

> « Le DXF est fourni avec deux coupes, idéalement le modèle devrait
> être construit en tenant compte des coupes également, par exemple
> pour placer des fenêtres. »

### Analyse

Un DXF d'archi typique contient plusieurs *vues* sur le même fichier :
- **Plan(s) d'étage** — projection 2D du dessus. Donne les positions
  XY des murs, l'épaisseur des murs (paires de lignes), les ouvertures
  (interruptions, blocs INSERT).
- **Coupes** — sections verticales du bâtiment. Donnent les
  élévations des niveaux, les hauteurs des ouvertures (allèges,
  linteaux), les hauteurs de plafond, les épaisseurs de dalles.

Notre tool UC1 Phase 1-3 (sessions f + i) ne traite que le plan. Les
coupes sont des entités dans le DXF qu'on ignore actuellement.

### Stratégie d'extraction

3 étapes :

**1. Segmenter le DXF en zones (plan vs coupes vs cartouches).**

Heuristique géographique : grouper les entités par cluster spatial
(DBSCAN sur les coords ou bounding-box overlap). Chaque cluster =
une vue. Identifier le type via :
- Le ratio aspect (plan ≈ ratio bâtiment, coupes plus allongées).
- La présence de symboles caractéristiques (lignes de niveau pour
  coupe, échelle graphique, cartouche).
- Le nom du layer dominant.

Alternative : convention de cartouche → titre de chaque vue lu via
OCR sur les MTEXT (« PLAN N0 », « COUPE A-A », etc.).

**2. Extraire les hauteurs des coupes.**

Sur chaque cluster identifié comme coupe :
- **Niveaux** : lignes horizontales longues + texte adjacent
  (`+3.00`, `Niveau 1`, etc.). Extraction des élévations absolues.
- **Ouvertures** : rectangles fermés ou paires de lignes horizontales
  sur les layers `A-GLAZ` / `A-DOOR`. Bottom = sill_height, top =
  head_height par rapport au niveau de référence.
- **Hauteurs de plafond** : différence entre niveaux successifs.

**3. Géo-référencement plan ↔ coupes.**

Chaque coupe est prise selon une **ligne de coupe** dans le plan
(une polyligne avec flèche, sur un layer typique `A-SECT` ou via un
symbole de référence type `1/A2.1`). Identifier la ligne dans le
plan → mapper l'abscisse X de la coupe à une position dans le plan.
Ensuite : pour chaque ouverture dans la coupe, retrouver son
homologue dans le plan via projection X → enrichir avec
sill/head extraits de la coupe.

### Architecture proposée

```
lib_floorplan/  (ou claude-in-revit.extension/lib/)
├── dwg_view_segmentation.py  # cluster spatial → vues séparées
├── dwg_section_reader.py     # extraction hauteurs depuis coupe
├── dwg_geo_referencing.py    # mapping plan ↔ coupes
└── dwg_ocr.py                # OCR MTEXT pour annotations
                               # (cf. note UC6 plan-d'après-image —
                               # même stack pytesseract possible)
```

Wrapper tier-2 :
- `dwg_inspect_views(file_path)` : retourne la liste des vues
  identifiées avec leur type + bbox.
- `dwg_extract_levels_from_section(file_path, view_index)` : niveaux
  + élévations depuis une coupe.
- `dwg_import_full(file_path, level_ref, ...)` : pipeline complet
  plan + coupes → murs + ouvertures avec hauteurs correctes.

### Difficultés majeures

1. **Identification automatique des coupes** sans annotations
   explicites est non-triviale. Heuristiques fragiles. Solution
   robuste : demander à l'utilisateur de pointer chaque vue
   (« la coupe A-A est dans la zone X=[50,100] Y=[-30,10] ») via
   un mini-dialog ou une convention layer.

2. **OCR sur MTEXT du DXF** pour lire les annotations
   (« +3.00 », « Niveau 1 », « EI60 »). Tesseract n'est pas
   nécessaire ici — le texte est déjà en clair dans le DXF, juste
   à parser. Plus simple que pour UC6 raster.

3. **Identification ligne de coupe dans le plan** :
   convention layer + symbole. Souvent fragile car les bureaux
   utilisent des standards variés.

### Phases d'exécution (estimation)

| Phase | Livre | Effort |
|---|---|---|
| 0 — segmentation vues | Cluster spatial + classification heuristique | ~1-2 j |
| 1 — extraction niveaux | Lignes horizontales + texte adjacent → Level KG | ~1 j |
| 2 — extraction hauteurs ouvertures | Rectangles dans coupes → sill/head | ~2 j |
| 3 — géo-ref plan ↔ coupe | Ligne de coupe + mapping X | ~2 j |
| 4 — orchestrateur full | `dwg_import_full` qui combine tout | ~1 j |

Phase complète : ~7-9 j homme. Hors scope V0.

### Déclencheur de reprise

- Cas client explicite (« j'ai un projet à modéliser depuis DXF
  archi complet »).
- OU après livraison V1 compliance / vision (UC6 raster) qui
  partagent des briques OCR et géo-ref.

### Préreqs identifiés

- `connects_at` peuplé au rescan (déjà dette ouverte) — utile pour
  reconnaître la topologie des murs aux jonctions de coupes.
- `Room.boundary_walls` calculé (déjà dette ouverte) — pour mapper
  pièces aux coupes.
- `catalog_list_views` côté Revit (déjà dette ouverte) — pour
  exporter les vues 2D des coupes que Revit générera automatiquement
  à partir du modèle 3D, comparable au DXF source.

### Quick win possible avant la full phase 4

Même sans extraire les hauteurs des coupes, on peut déjà :
- Détecter les **lignes horizontales longues annotées** dans un
  DXF (qu'il soit plan ou coupe) → identifier les **niveaux** et
  les créer automatiquement via `levels_create_many` (à
  développer aussi en passant : pour l'instant on a `levels_create`
  solo).

C'est une fraction de la phase 1 qui apporte une valeur immédiate.
Effort ~½ j.

---

## 2026-05-12 (session i) — Validation runtime UC1 DWG : routing tier-2, centerline fallback, delete_many

### Contexte & objectif

Suite directe de session h. Test 6 de validation runtime : ingest
DWG/DXF sur un fichier `.dxf` réel (`Projet4 - Plan d'étage - Niveau 0.dxf`).
4 issues remontées en cascade, toutes adressées.

### Issue 1 : routing tier-2 inexistant → tools dwg_* invisibles au LLM

Les tools `dwg_inspect`, `dwg_classify`, `dwg_import_walls` sont déclarés
`tier=2`. Le pushbutton appelait `run_turn(tier_max=1)` en dur — le LLM
ne voyait jamais ces tools. Premier prompt utilisateur : « je n'ai pas
d'outil pour lire des fichiers ». Dette d'infrastructure : DESIGN
prévoyait un `routing.py` avec `ROUTING_RULES`, jamais implémenté.

**Fix minimal** : helper `preprocess.infer_tier_max(prompt)` qui détecte
les keywords DWG/DXF via regex. `prompt.pushbutton/script.py` calcule
`tier_max` dynamiquement à partir du prompt avant `run_turn`. Couvre
les phrases « importe dxf », « inspecte dwg », « ingest plan cad », etc.

À étendre quand d'autres domaines tier-2 arrivent (compliance UC8,
vision UC6). Pour V0 c'est la solution minimale.

### Issue 2 : bytecode .pyc cached bloquait les fixes hot-reload

`AttributeError: module 'lib.preprocess' has no attribute 'infer_tier_max'`
au prochain test. Le fichier `.pyc` cached était postérieur à mes
modifs (race entre pushbutton click et save). Python ne recompilait pas.

**Fix** : purge manuelle de `__pycache__/preprocess.cpython-312.pyc`.
Note pour la suite : pyRevit / CPython embarqué peut garder du
bytecode stale. Si une modif n'est pas reflétée au prochain click,
purger `__pycache__` en premier réflexe.

### Issue 3 : `dwg_classify` preview "20 of 29" → LLM reconstitue 9 murs manuels

Le tool tronquait le preview de walls à 20 et émettait :
```
"walls_truncated": true,
"note": "Preview limited to 20 walls of 29. Apply via dwg_import_walls to commit all."
```

Le LLM a interprété « 20 of 29 » comme **« il en manque 9 »** au lieu
de « le preview est tronqué mais l'import en commit 29 ». Du coup :
1. Il a appelé `dwg_import_walls` → 29 murs créés correctement.
2. Il a vu une « différence » de 9 et a tenté de reconstruire les 9
   manquants via `walls_create_many` en extrapolant depuis le pattern.
3. Résultat : **38 murs avec 9 doublons sur les fenêtres**. Revit
   signale 9 avertissements « se chevauchent ».

**Fix** :
- `preview_limit=100` (au lieu de 20). La plupart des plans tiennent
  sans troncature. Sur ton DXF (29 walls), pas de troncature du tout.
- Au-delà de 100, note explicite : « **`dwg_import_walls` créera la
  totalité (N), PAS seulement les 100 affichés ici. Ne reconstitue
  PAS les murs manquants manuellement** ».

C'est un **bug d'UX du tool**, pas du LLM. Lesson : tout message qui
peut être lu de deux façons sera lu de la mauvaise.

### Issue 4 : cloisons internes en simple-trait → fallback centerline (UC1 Phase 3)

Après import + cleanup des doublons, l'enveloppe 10×20m apparaît mais
**aucune cloison intérieure** n'est créée. Inspection du DXF : 4
segments orphelins à x=3.70m, fragmentés par 3 portes (gaps de
0.20m), longueur totale 14.79m. Une **cloison interne en simple-trait**
(centerline only, pas de paire) — convention courante pour les
cloisons légères.

Notre pair-detection ne peut pas les détecter (il manque la 2e face).
C'était identifié comme Phase 3 du roadmap UC1 dans la note d'intention.

**Fix livré** (`lib/dwg_classifier.py`) :
- **`_merge_collinear_segments(segments, max_gap_m=0.20)`** : groupe les
  segments par droite portante (clé `(angle_bin, perp_signed_from_origin)`),
  fusionne ceux qui se chevauchent ou ont un gap ≤ `max_gap_m`.
  Absorbe les portes intérieures qui interrompent une cloison continue.
- **`detect_centerline_walls(orphans, thickness_m=0.10, min_length_m=0.5)`** :
  fusion collinéaire des orphelins + filtrage par longueur min
  (exclut les épaulements de fenêtres). Synthétise des WallCandidate
  avec `confidence=0.6` (inférence partielle).
- **`_segment_in_shadow_of_pair`** : filtre anti-doublon. Rejette les
  centerline candidates qui sont quasi-parallèles à un pair-wall
  existant, perp distance < 0.30m, ET overlap projeté ≥ 0.80. Évite
  de re-créer des doublons aux endroits où la pair detection a déjà
  capté un mur. `overlap_threshold=0.80` (pas 0.30) : avec un seuil
  bas, on filtrait à tort la vraie cloison qui chevauche partiellement
  un mur intérieur fragmenté.

**Flags propagés** dans `dwg_classify` + `dwg_import_walls` :
`include_centerline`, `centerline_thickness_m`, `centerline_min_length_m`,
`centerline_max_gap_m`. Tous on par défaut (cas d'usage standard).

**Validation runtime** : sur le Projet4 DXF :
- Avant fix : 29 paires + 124 rejected. La cloison à x=3.70 dans les
  rejected.
- Après fix : 29 paires + 3 centerlines = 32 walls. Centerlines : la
  vraie cloison de 14.79m + 2 trumeaux de 1.20m entre fenêtres sur
  les façades Est/Ouest. Pas de doublon.

### Issue 5 (la plus critique) : suppression en masse manquante

Pendant que je travaillais sur le centerline, le user a tenté un
cleanup global du projet (98 walls + 100+ openings) sans tools
`*_delete_many`. Résultat : **~200 round-trips API** (un
`walls_delete` par mur, un `openings_delete` par ouverture).
215K input tokens, $$$. Demande critique consignée : « la suppression
en masse doit absolument être implémentée ».

**Fix livré** :
- **`walls_delete_many(items)`** dans `lib/tools/walls.py`.
- **`openings_delete_many(items)`** dans `lib/tools/openings.py`.
- **`rooms_delete_many(items)`** dans `lib/tools/rooms.py`.

Caractéristiques communes :
- **Items polymorphes** : accepte `["wall_001", "wall_002"]` (strings
  bruts) OU `[{"llm_id": "wall_001"}, ...]` (dicts). Le LLM oscille
  entre ces deux formats selon contexte ; `_validate_delete_item`
  tolère les deux.
- **Tolérance aux ElementId périmés** : si `doc.GetElement(eid)`
  retourne None (orphelin), soft-delete KG seulement sans crash.
  Signale `revit_already_gone: [llm_id]` dans le payload.
- **Tolérance aux refus Revit** (exceptions sur `doc.Delete`) :
  soft-delete KG quand même, signale dans `revit_already_gone`.
- Réponse compacte : `{count, deleted_revit, deleted_kg_only,
  revit_already_gone, deleted_at_turn, revit_modified}`.

`bulk_apply_to_filter` peut désormais router vers ces tools :
```
bulk_apply_to_filter(
    filter={"type": "Wall"},
    target_tool="walls_delete_many",
    tool_args={},
)
```

**Gain attendu** : ~200 round-trips → 2-3 (un delete_many par catégorie,
ou un bulk_apply_to_filter par filtre).

### Validation

- `pytest -q` : **348 verts** (342 → +6 nouveaux + 1 fixé). Aucune
  régression.
- Test centerline runtime sur Projet4 DXF : 32 walls détectés incluant
  la cloison interne. Reste à valider l'import effectif dans Revit
  (commit puis user relance).
- Test delete_many runtime : à valider quand user reprend.

### Dettes ouvertes

- **Routing tier-2 propre** : `preprocess.infer_tier_max` est une
  solution minimale par regex. Quand d'autres domaines tier-2 arrivent
  (compliance, vision), il faudra factoriser dans un vrai `routing.py`
  avec `ROUTING_RULES` map keyword→liste tools.
- **Helper `get_element_or_raise`** non étendu aux walls/columns
  (toujours, dette session g). Avec `walls_delete_many` qui maintenant
  fait le check None manuellement et tolère, ce n'est plus critique
  pour `_delete`. Mais setters / movers walls/columns restent
  exposés au crash NoneType.
- **Test runtime delete_many** non lancé (user a déjà cleané via le
  vieux pattern ~200 calls).
- **Test runtime ré-import DXF avec centerline** : à venir.

### Bonus : .pyc stale gotcha

À ajouter dans CLAUDE.md gotchas CPython : **si une modif Python
n'est pas reflétée au prochain pushbutton click malgré le hot-reload
(« nouveau process à chaque click »)**, vérifier
`<extension>/lib/__pycache__/` et purger le `.pyc` du module modifié.
Cas observé en session i : `preprocess.cpython-312.pyc` cached
post-modif via une race condition save/click → AttributeError au next
import. CPython ne recompile pas si le `.pyc` mtime ≥ `.py` mtime.

---

## 2026-05-12 (session h) — Validation runtime suite : purge, bulk filter, fix nommage variants

### Contexte & objectif

Suite directe de session g (validation runtime). Tests 4 (purge) et 5
(bulk filter) cumulés sur le même projet Revit. 3 issues remontées,
toutes adressées.

### Issue 1 : purge ne capture pas les variants créés via API à noms personnalisés

Bug rapport utilisateur : `openings_purge_unused_variants` détectait
uniquement les variants avec marqueur de nom `[auto h<NN>cm]`. Or
le LLM peut aussi créer des variants via le tool explicite
`openings_create_type_variant` avec un nom personnalisé (sans
marqueur). Ces variants restaient « préservés » par la purge même
orphelins.

**Fix** :
- Nouveau reserved attr KG `_origin` (`project_kg.ORIGIN`). Posé à
  `"api"` par `_create_type_variant_internal` lors de la création
  d'un variant (via auto-découple OU via tool explicite).
- `_is_auto_variant` étendu : matche le marqueur de nom **OU**
  `_origin == "api"`. Union logique conservatrice : un type importé
  par `full_rescan` n'a ni l'un ni l'autre → préservé.
- Méthodes `kg.set_origin(llm_id, origin)` et `kg.get_origin(llm_id)`
  symétriques à `set_revit_id` / `get_revit_id`.

### Issue 2 : variants orphelins legacy sans tag `_origin`

Cas rétrocompat : variants créés AVANT le tag `_origin` (sessions
c–g), portant des noms personnalisés sans marqueur. Le tool ne peut
pas les distinguer des types du template Revit que l'utilisateur
veut garder.

**Fix** :
- Flag `include_unmarked: bool = False` (défaut conservateur) sur
  `openings_purge_unused_variants`. Quand `True`, la purge cible
  *tout* FamilyType orphelin de la catégorie (avec ou sans
  marqueur / tag). Documentation explicite du risque (purge aussi
  les types template non utilisés).
- L'utilisateur peut le déclencher en demandant explicitement « purge
  TOUS les types orphelins, même ceux sans marqueur auto ».
- Validation runtime : 4 orphelins legacy purgés, 1 utilisé préservé
  (`scanned=5, purged=4, kept=1`). 1 seul tool call.

### Issue 3 : Revit refuse les caractères `[` et `]` dans les noms de FamilySymbol

**Bug critique** caché depuis session c. La convention de nommage
`<src> [auto h<NN>cm]` viole les règles de nommage Revit : `[` et
`]` sont des caractères réservés pour les paramètres d'instance dans
les noms de types. `Symbol.Duplicate(name)` avec un nom contenant
ces caractères échoue silencieusement (retourne None ou un symbol
invalide), d'où la confusion sur les tests précédents.

**Pourquoi caché jusqu'ici** :
- Session c : tests unitaires KG-only (pas de Revit) → bug non vu.
- Session g test 3b : a réutilisé un variant `[auto h100cm]` créé
  lors d'une tentative *antérieure* en KG-only (où Revit n'est pas
  appelé). Le variant existait côté KG mais probablement pas côté
  Revit. Le swap a marché parce que `instance.Symbol = old_symbol`
  (rien n'a vraiment bougé Revit-side). On a interprété le succès
  comme une preuve d'idempotence — c'était en fait un coup de
  chance lié à un état pollué.
- Session h test 5 : tentative de création d'un *nouveau* variant
  `[auto h150cm]` côté Revit → échec. LLM a contourné en réutilisant
  un variant manuel pré-créé. Bravo à lui, mais le code doit
  fournir un chemin propre.

**Fix** :
- `_variant_name` : retour de `<src> (auto h<NN>cm)` (parenthèses
  autorisées par Revit) au lieu de `<src> [auto h<NN>cm]`.
- `_AUTO_VARIANT_MARKER_RE` étendu pour matcher **les deux**
  conventions (`\(auto h\d+cm\)|\[auto h\d+cm\]`) → rétrocompat
  avec variants existants nommés à l'ancienne.
- Le tag `_origin = "api"` (Issue 1) rend ce détail nominal moins
  critique fonctionnellement, mais propre côté browser Revit.

### Bonus : UX dialog CRLF

`lib/ui_dialogs.py` rendait tout le texte sur une seule ligne car
WinForms `TextBox.Text` n'interprète que les CRLF (`\r\n`) comme
sauts de ligne, pas les LF seuls (`\n`, convention Python). Fix :
normalisation `\r\n` / `\r` → `\n` puis `\n` → `\r\n` avant
assignment au `TextBox`. Le clipboard reçoit la même version
normalisée pour cohérence affichage-collage.

### Validation

- Test 4 (purge runtime) : ✓ 4 orphelins purgés, 1 utilisé préservé,
  1 tool call.
- Test 5 (bulk filter — partiel) : LLM a choisi la route items-based
  (`openings_set_sill_height_many`) parce que l'autoscan KG lui
  pré-fournissait la liste. Rationnel. **`bulk_apply_to_filter`
  reste non testé en runtime** — sera utile pour les cas où l'autoscan
  ne pre-fetche pas (filtres composés type+level_ref+type_ref). À
  reprendre.
- Test 6 (DWG ingest) : non démarré, prochaine session.
- `pytest -q` : 332 verts. Aucune régression.

### État final & reste à faire

**Acquis session h** :
- Tag `_origin` (reserved attr) ✓
- Flag `include_unmarked` ✓
- Convention de nommage variants parenthèses ✓ + rétrocompat
- UX CRLF dialog ✓
- 332 verts ✓

**Dettes ouvertes** :
- `bulk_apply_to_filter` non testé runtime — prochaine session si
  on déclenche un cas où l'autoscan ne pre-fetche pas.
- Test 6 (DWG ingest) à venir.
- Migrations futures : tag `_origin` perdu au rescan (le
  `full_rescan` n'inspecte pas le browser Revit pour distinguer
  variants auto vs manuels). Acceptable car au rescan le KG est
  reconstruit depuis Revit, et les variants nommés `(auto h<NN>cm)`
  sont reconnus via le marqueur de nom même sans tag.

---

## 2026-05-12 (session g) — Validation runtime cumulée : 6 bugs Revit-side + UX + system prompt

### Contexte & objectif

Première session de validation runtime end-to-end après l'enchaînement
sessions a → f (rooms+levels, setters_many, auto-découple, purge,
bulk, DWG). Test phare visé : reproduction du bug rapporté
2026-05-12 matin (« head=2m sur fenêtres → sill recompute parasite »)
avec le code post-session c (auto-découple) pour confirmer le fix
en prévention. Plan de test à 7 scénarios (smoke + 6 sessions).

**Modèle test** : Projet1/2 vierge, niveau SS01 à -3m, 4 murs
rectangle 5×4m, 4 fenêtres standard.

### Bugs trouvés et fixés

La validation runtime a remonté **6 bugs sur le path Revit** que la
suite unitaire KG-only ne couvre pas. Tous fixés avant push.

**1. `_maybe_decouple` hors `rp.transaction`** (sessions c et b).
Le pré-flight auto-découple appelait `_create_type_variant_internal`
+ `_swap_to_type_internal` *avant* l'ouverture de la Tx Revit. Or
ces fonctions font des mutations Revit (`source_symbol.Duplicate`,
`instance.Symbol = new_symbol`) qui exigent une Tx ouverte. Hors-Tx
ces appels Revit retournent silencieusement `None` au lieu de lever
→ cascade `AttributeError: NoneType` sur le `.Id.Value` suivant.

Cause racine : refactor session c, manque de runtime check.
Fix : déplacer `_maybe_decouple` *dans* le `with rp.transaction(...)`
des 4 setters openings (solo + many). KG-only path reste exécuté hors-Tx
(le helper a son propre branchement KG).

**2. `NewFamilyInstance` overload 4-args : level par défaut Niveau 1.**
`doc.Create.NewFamilyInstance(point, symbol, host, structuralType)`
sans Level explicite → Revit assigne le **premier level du projet**
(typiquement Niveau 1 à élévation 0) comme Reference Level de
l'instance, *au lieu* d'hériter du level du host_wall. Sur un mur
hosté sur SS01 (-3m), la fenêtre se retrouvait à
`Reference Level=Niveau 1 + sill 1m = z=1m monde` → « un étage trop
haut » visuellement.

Diagnostic : l'utilisateur a confirmé `wall.Contrainte inférieure =
SS01` via inspection visuelle, donc le mur était bon. Le décalage
venait de la fenêtre.

Fix : `NewFamilyInstance(XYZ, FamilySymbol, host, Level,
StructuralType)` (overload 5-args) avec `Level` résolu depuis
`level_ref` du host_wall.

**3. XYZ.Z avec l'overload 5-args : sémantique world, pas relatif.**
Mon premier fix passait `XYZ.Z = 0` avec le Level explicite, en
supposant que Revit ajoutait `Level.Elevation` automatiquement.
**Erreur d'interprétation** : avec l'overload 5-args, Revit attend
le XYZ en **coordonnées monde absolues**. Sur SS01, `XYZ.Z=0` plaçait
la fenêtre à z=0 monde, hors emprise du mur (qui va de -3 à -0.3)
→ erreur Revit « occurrences de … ne coupent rien », fenêtres
flottantes invisibles dans le mur.

Trace décisive : utilisateur a noté « ça fonctionne en les créant
au niveau 0 ». Sur Niveau 1 (elev=0), `XYZ.Z=0` et `XYZ.Z=level_elev`
coïncidaient — d'où l'illusion que `0` était correct.

Fix : `XYZ.Z = rp.meters_to_internal(level_elev_m)` (monde). Revit
calcule sill = XYZ.Z − Level.Elevation = 0 à la création, puis
`INSTANCE_SILL_HEIGHT_PARAM.Set(sill_height_m)` impose la sill voulue.

**4. `doc.GetElement(eid)` retourne `None` silencieusement sur
ElementId périmé.** Quand le KG porte un binding `_revit_id` vers
un élément Revit déjà supprimé (orphelin causé par un workflow hors
pipeline : utilisateur qui supprime via UI Revit, ou crash partiel
d'une session précédente), `doc.GetElement(eid)` ne lève pas — il
retourne juste `None`. Cascade : `element.get_Parameter(...)` →
`AttributeError: 'NoneType' object has no attribute 'get_Parameter'`.
Le LLM diagnostiquait à tort un « problème de session ElementId »
qui n'existe pas en réalité.

Fix : helper centralisé `revit_primitives.get_element_or_raise(doc,
eid, llm_id, kind)`. Renvoie l'`Element` ou lève une `ValueError`
actionnable : *« Revit binding stale for window window_X (ElementId
N): element not found in document. Run Refresh KG to purge orphan
KG nodes, then retry. »* Appliqué dans les 4 setters openings +
`_swap_to_type_internal`. À étendre aux walls/columns à l'occasion
(dette).

**5. `levels_create` ne créait pas le FloorPlan associé.** L'API
`Level.Create(doc, elev)` côté code crée le Level mais *pas* la vue
Plan d'étage, contrairement à l'UI ruban Revit qui propose
automatiquement la création. Du coup le nouveau niveau apparaissait
en élévation et en arborescence Vue, mais pas dans la liste Plans
d'étage — UX cassée par rapport à l'attente utilisateur.

Fix : flag `create_floor_plan=True` par défaut sur `levels_create`
qui appelle `ViewPlan.Create(doc, vft.Id, level.Id)` dans la même
Tx après le Level. ViewFamilyType FloorPlan résolu via
`FilteredElementCollector(doc).OfClass(ViewFamilyType)`. Nouveau
tool `levels_create_floor_plan(llm_id)` pour réparer un niveau
existant (cas du SS01 créé pré-fix). Le ViewPlan n'est pas bindé au
KG (vues = V1, cf. dette `catalog_list_views`).

**6. LLM passant `preserve_sill=False` par accident.** Le LLM,
voyant les flags `preserve_sill` / `preserve_head` dans la docstring
des setters et confondant leur sémantique, a passé `preserve_sill=False`
sur un appel `openings_set_head_height_many`. Conséquence : pas de
pré-flight `_maybe_decouple`, `param.Set(head=2.0)` direct → Revit
recompute sill = head − family.opening_height = 2.0 − 1.2 = 0.8m
(au lieu du 1.0m attendu). C'était **exactement** le bug rapporté
2026-05-12 matin que session c devait éviter — mais en runtime,
pas reproductible côté tests parce que les tests passent toujours
le flag par défaut.

Fix : durcissement du `_STATIC_SYSTEM_PROMPT` dans `prompt.pushbutton`
avec une règle explicite : « **NE PASSE JAMAIS** les flags `preserve_*`
sauf demande utilisateur explicite (« accepte que l'allège bouge »).
Mauvaise manipulation = bug 2026-05-12. »

### UX

- **`lib/ui_dialogs.py`** : `show_selectable_text(title, body)` —
  fenêtre WinForms `Form` + `TextBox` (Multiline, ReadOnly, Vertical
  scroll, Consolas) + bouton « Copier » qui pousse au clipboard +
  bouton « Fermer » + Esc=Close + Ctrl+A select-all. Préserve le
  clipboard utilisateur (pas d'auto-copy par défaut).
- `prompt.pushbutton/script.py` : `_show()` (réponse LLM finale)
  passe par `show_selectable_text` ; `_show_error()` reste sur
  `TaskDialog` (chemin d'erreur, pas de dépendance autre).
- `refresh_kg.pushbutton/script.py` : pareil. Summary étendu avec
  les nouveaux compteurs **Door types / Doors / Window types /
  Windows / Rooms** + `preserved_llm_ids` (manquaient sur le
  rendering du dialog, alors que les données étaient déjà calculées
  par `kg_sync.full_rescan` depuis sessions 4 et a). Ligne « skipped »
  rendue compacte (only-nonzero).

### Validation runtime

Après l'enchaînement des fixes, le scénario phare **test 3b
auto-découple** a passé end-to-end :

- Setup : 4 fenêtres sur SS01, sill=1.0 head=2.20, type
  `0.60 × 1.20m (Appui en aluminium)`, opening_height=1.20m.
- Prompt : « passe le linteau à 2.0m, préserve l'allège à 1.0m ».
- Résultat : **`decoupled_count=4, auto_variants_created=0`** —
  les 4 fenêtres swappées vers un variant `[auto h100cm]`
  *préexistant* (créé lors d'une tentative antérieure puis réutilisé
  → idempotence). sill=1.00m, head=2.00m réels dans Revit.

C'est exactement le comportement spécifié par session c.

### État final & reste à faire

**Acquis session g** :
- 6 bugs runtime fixés ✓
- Helper `get_element_or_raise` ✓
- `levels_create` + FloorPlan + tool de réparation ✓
- Dialog sélectionnable + Copier ✓
- Refresh KG summary complet ✓
- System prompt durci (level naming + preserve_*) ✓
- 332 tests verts, aucune régression ✓
- Bug rapporté 2026-05-12 matin **réglé runtime end-to-end** ✓

**Dettes ouvertes** (héritage + nouveau) :
- Étendre `get_element_or_raise` aux setters walls/columns
  (même classe de bug potentiel).
- Validation runtime restante : test 4 (purge), test 5 (bulk filter),
  test 6 (DWG ingest). À couvrir à la prochaine session.
- Drift utilisateur hors pipeline (events `DocumentChanged`) toujours
  ouvert.
- `boundary_walls` Rooms, `connects_at`, `catalog_list_views`
  toujours en dette (préreq auto-cotation).

**Leçon générique** : la validation runtime sur projet Revit réel
remonte des bugs que la couverture tests KG-only ne peut pas
capturer (path Revit mocké au mieux, jamais exécuté). Pattern à
réinjecter dans la roadmap : prévoir une session runtime *après*
chaque livraison V0 majeure, pas seulement après V0 Sem.4-5.

---

## 2026-05-12 (session f) — UC1 DWG/DXF ingest : reader + classifier + tools (V0 Sem.4-5)

### Contexte & objectif

§9 V0 Sem.4-5 : dernier morceau, UC1 = ingest DWG/DXF avec `dwg_reader.py`
+ `dwg_classifier.py` + tools `dwg_import_*`. Pose la mécanique
préprocesseur → wall segments → `walls_create_many` qui sera réutilisée
pour UC6 raster (cf. note d'intention plan d'après image).

Cadrage utilisateur en début de session (3 questions structurantes
posées) :
- Formats : **DXF + DWG via ODA File Converter** (utilitaire externe gratuit).
- Détection murs : **paires de lignes parallèles + centerline** (pas
  paire = orphelin rejeté).
- Layer mapping : **heuristique nom + override LLM/user** (pas
  explicite-only ni persistence).

### Décisions

1. **`ezdxf` 1.4.3** ajouté à `pyproject.toml` + installé en venv.
   Pure-Python. Le pyRevit embarqué nécessitera un `pip install ezdxf`
   au déploiement (procédure CLAUDE.md déjà documentée).

2. **`config.oda_converter_path()`** : résolution 3 sources (fichier
   `~/.config/claude-in-revit/oda_converter_path.txt` → env var
   `ODA_FILE_CONVERTER` → glob sur paths standard Windows
   `C:\Program Files\ODA\ODAFileConverter*\`). Retourne `None` si rien
   trouvé. **Pas d'exception au démarrage** — seul un `.dwg`
   effectivement présenté déclenche `ConfigError` actionnable (workflow
   DXF-only ne doit pas exiger ODA installé).

3. **Conversion d'unités à la lecture** via `$INSUNITS` du DXF. Table
   explicite `_DXF_UNIT_TO_METERS` (0..10 = unitless, inch, feet,
   miles, mm, cm, m, km, etc.). `scale_override` exposé pour les
   fichiers unitless ou mal annotés. Le KG et tout le pipeline en
   aval travaillent toujours en mètres.

4. **`DwgEntity` dataclass agnostique** du backend — `kind`, `layer`,
   `coords` (liste de points 3D en mètres), `attrs`. V0 supporte
   LINE / LWPOLYLINE / POLYLINE / ARC / CIRCLE / INSERT / TEXT / MTEXT.
   SPLINE, HATCH, DIMENSION, IMAGE → `_SkipEntity` silencieux (V1+).
   LWPolyline `bulge` ignoré (segments droits uniquement).

5. **Heuristique layer name** : 17 patterns regex multi-langue FR + EN
   + AIA. Mappe noms vers `"wall" | "door" | "window" | "text" | "ignore"`.
   Normalisation `_/-` → espace avant matching, parce que `\b` Python
   considère `_` comme word char (gotcha rencontré au test) — donc
   `\bMURS\b` aurait raté `MURS_PORTEUR` sans normalisation.
   Ê/É/È/Ë acceptés pour `FENÊTRE`.

6. **Pair detection algorithme** :
   - O(N²) sur segments d'un layer wall. Acceptable jusqu'à ~quelques
     milliers de lignes (les plans archi typiques sont < 1000).
   - Conditions pour qu'une paire `(i, j)` forme un mur :
     - même layer ;
     - angles parallèles mod π à `angle_tol_rad` (défaut 2°) ;
     - distance perpendiculaire dans `[0.05, 0.50] m` ;
     - overlap projeté ≥ 50% du segment court.
   - Synthèse : centerline = milieux appariés (gestion du cas
     dessin en sens inverse via projection ordering), thickness =
     distance perpendiculaire, confidence = overlap ratio.
   - Lignes orphelines → `rejected` avec raison explicite.

7. **3 tools tier-2** (chargés via routing keyword `dwg` / `dxf` /
   `importe` / `plan d'archi`) :
   - `dwg_inspect(file_path)` : preview layers + `suggested_role`.
   - `dwg_classify(file_path, layer_mapping)` : preview walls détectés.
   - `dwg_import_walls(file_path, level_ref, wall_type_ref,
     layer_mapping, dx_m, dy_m, height_m, ...)` : orchestre classify
     + dispatch direct vers `walls_create_many` (sans nested
     transaction, même pattern que `bulk_apply_to_filter` session e).

8. **`max_walls` garde-fou** (défaut 500) — refus du batch si la
   classification produit plus de N candidats. Évite l'import
   accidentel d'un layer sur-segmenté qui créerait des milliers de
   murs parasites.

9. **Type unique en V0 phase 1** : tous les murs créés avec le même
   `wall_type_ref`. Phase 2 : mapper `thickness_m` → `wall_type_ref`
   compatible (lookup dans le catalog des WallTypes par épaisseur).

### Phase 1 — `config.py` extension

- Ajout `oda_converter_path() -> Optional[Path]` (file → env → glob).
- Constantes `ODA_PATH_FILENAME`, `ODA_ENV_VAR`, `_ODA_DEFAULT_GLOBS`.
- Renvoie `None` plutôt que de lever — `dwg_reader` lève
  `ConfigError` actionnable seulement quand un `.dwg` est rencontré.

### Phase 2 — `lib/dwg_reader.py`

- `DwgEntity` dataclass.
- `parse(file_path, scale_override=None) -> (entities, meta)`.
- `_dwg_to_dxf_via_oda(dwg_path) -> Path` : shell-out subprocess vers
  ODA Converter CLI, écrit dans temp dir, retourne le DXF généré.
  Timeout 120s, vérifie présence du DXF en sortie (ODA Converter ne
  renvoie pas toujours un return code propre).
- `_convert_entity(entity, factor)` : dispatch par `dxftype()`.
  `_SkipEntity` pour les types non supportés.
- `entities_by_layer(...)` helper.

### Phase 3 — `lib/dwg_classifier.py`

- `_LAYER_ROLE_PATTERNS` : 17 regex + rôles, multi-langue.
- `suggest_layer_role(layer_name)` : normalisation + match.
- `annotate_layers(meta["layers"])` : mutate-in-place.
- `Segment` dataclass, `extract_straight_segments(entities, ...)`.
- `WallCandidate` dataclass.
- `detect_wall_segments(segments, ...)` : O(N²) avec early-exit sur
  used. Renvoie `(walls, rejected)`.
- `Classification` dataclass.
- `classify(entities, layer_mapping, ...)` : entrée publique.

### Phase 4 — `lib/tools/dwg_import.py` (3 tools tier-2)

- `dwg_inspect` : preview layers + suggested_role.
- `dwg_classify` : preview walls (KG / Revit read-only).
- `dwg_import_walls` : commit avec dispatch direct vers
  `walls_create_many` (registry lookup, `entry.fn(kg, doc, items)`).

### Phase 5 — Fixtures DXF synthétiques + tests (+41)

`tests/test_dwg.py` :

- **Génération DXF programmatique** via `ezdxf` au temps d'exécution.
  Pas de binaires DXF versionnés. Helper `_make_rectangle_room_dxf`
  produit une pièce 5×4m avec 4 murs orthogonaux d'épaisseur 0.20m
  (8 lignes paires) + optionnellement extras (orphelin sur WALL, ligne
  FURNITURE à ignorer).
- **Reader** (4 tests) : parsing simple, conversion mm → m,
  `scale_override` cumulé, `ConfigError` actionnable sur .dwg sans ODA.
- **`suggest_layer_role`** paramétrique (21 cas) : EN, FR, AIA, casse,
  Ê/É, MOBILIER/FURNITURE/HATCH ignorés, layer `"0"` et noms
  arbitraires → None.
- **`annotate_layers`** : mutation in-place + chaînage.
- **Pair detection** (6 tests) : pair orthogonale, refus trop loin,
  refus non-parallèle, refus no-overlap, dessin sens inverse, refus
  layers différents, pièce complète 4 walls.
- **`classify`** (2 tests) : mapping → walls, layers non-mappés
  silencieux (orphelins WALL signalés).
- **Tools** (6 tests) : inspect (layer summary + suggested_role),
  classify (preview), import_walls (création KG), translation dx/dy,
  garde-fou max_walls, mapping ignore = no-op.

### Bugs rencontrés + fix

1. **`\bMURS_PORTEUR` ne matche pas `\bMURS\b`.** Cause racine : `_`
   est `\w` en Python regex, donc pas de word boundary après "MURS".
   **Fix** : normaliser `_/-` → espace dans `suggest_layer_role`.
2. **`FENÊTRES` (Ê circonflexe) ne matche pas `[EÉ]`.** Cause racine :
   character class limité. **Fix** : `[EÉÈÊË]`.
3. **`A-WIND-FRAME` ne matche pas `\bA[-_]?WIND\b` après
   normalisation.** Cause racine : après normalisation `-/_ → espace`,
   le pattern `[-_]?` devient inutile mais la condition d'adjacence
   immédiate de A et WIND est rompue. **Fix** : patterns AIA-style
   utilisent `A ?` (espace optionnel) au lieu de `A[-_]?`.

### Validation

- `pytest -q` (suite complète) : **330 verts en 11.52s** (289 → +41).
- 41 nouveaux tests, dont 21 paramétriques sur `suggest_layer_role`.
- Aucune régression sur les 289 tests existants.

### État final & reste à faire

**Acquis session f (UC1 V0 phase 1)** :
- `config.oda_converter_path()` ✓
- `lib/dwg_reader.py` : parsing DXF + DWG-via-ODA, 8 kinds entités,
  conversion `$INSUNITS` ✓
- `lib/dwg_classifier.py` : heuristique 17 patterns multi-langue,
  pair detection O(N²) + reject reasons ✓
- 3 tools tier-2 : `dwg_inspect`, `dwg_classify`, `dwg_import_walls` ✓
- 41 tests, **330 verts** ✓
- `ezdxf>=1.3` dans `pyproject.toml` ✓
- §9 V0 Sem.4-5 (UC1+UC7) **bouclé**.

**Dettes ouvertes (héritage)** :
- DWG support testé runtime (nécessite ODA installé sur poste de dev) —
  pas de CI machine avec ODA disponible. Validation manuelle au
  prochain run terrain.
- Drift utilisateur hors pipeline (`DocumentChanged`).
- `boundary_walls` Rooms V1 compliance.
- `connects_at` peuplé au rescan.
- `catalog_list_views`.

**Phases UC1 ultérieures** (V0 → V1) :
- **Phase 2** : openings (portes = arcs sur layer DOOR, fenêtres =
  double-trait sur layer WINDOW). Réutilise `_maybe_decouple` session
  c pour les fenêtres dont le head/sill diverge.
- **Phase 3** : map `thickness_m` → `wall_type_ref` compatible (lookup
  WallType par épaisseur dans le catalog).
- **Phase 4** : import polylines fermées comme room boundaries (préreq
  `Room.boundary_walls`).
- **Phase 5** : INSERT blocks → familles Revit (porte, fenêtre,
  mobilier). Sophistiqué — mapping block_name → FamilyType.

**Suite immédiate (V0 → V1 ou V0.5)** :
- Validation runtime Revit cumulée (rooms+levels session a,
  setters_many b, auto-découple c, purge d, bulk e, DWG f) — idéalement
  sur un projet concret pour mesurer les gains.
- Préreqs auto-cotation (`connects_at` + `boundary_walls` +
  `catalog_list_views`) — débloquant multiple notes d'intention.
- V1 compliance UC8 — le KG projet est maintenant solide, le moment
  est bon pour attaquer le modèle compliance.

---

## 2026-05-12 (session e) — `tools/bulk.py` : filter-based dispatch (UC7 V0 Sem.4-5)

### Contexte & objectif

Référence §9 V0 Sem.4-5 du DESIGN : `tools/bulk.py` avec
`apply_to_filter` et `change_param_bulk`. Couvre aussi la dette 1 de
la session b (« filter-based bulks reporté à `tools/bulk.py`
Sem.4-5 »). Cas d'usage central : « passe toutes les fenêtres du N01
à sill=0.80 » devient un seul tool call au lieu de la chaîne
`catalog_list_windows` → construire items → `*_many`.

### Décisions

1. **Items-based reste primary**, filter-based est un *pendant*. Les
   `*_many` livrés session b restent les point d'entrée explicites
   (LLM construit la liste). `bulk_apply_to_filter` est l'ergonomie
   filter-based résolue côté KG.

2. **Filter dict plat, AND implicite, keys whitelistées.** Pas de DSL
   `{"and": [...], "or": [...]}` — overkill pour V0. Keys autorisées :
   `type`, `level_ref`, `type_ref`, `host_wall_ref`, `category`,
   `name`, `name_contains`, `name_regex`. Key inconnue → `ValueError`
   explicite (pas de match silencieux à zéro sur faute de frappe).

3. **Match strict sur `_type` quand `type` fourni.** Optimisation O(N)
   sur la KG via `find_by_type`. Si absent, fallback `O(N_total)` —
   acceptable jusqu'à quelques milliers de nodes.

4. **Soft-deleted toujours exclus**, même quand l'utilisateur ne le
   demande pas. Cohérent avec `find_by_type` partout ailleurs.

5. **Dispatch direct via `entry.fn(**kwargs)`, pas via
   `dispatch_tool_use`.** Audit fait : `dispatch_tool_use` ouvre une
   `kg.transaction()` qui persiste à la sortie. Si on dispatch un
   `*_many` cible depuis `bulk_apply_to_filter` *via* `dispatch_tool_use`,
   on a Tx imbriquée → l'inner `kg.persist()` écrit sur disque avant
   l'éventuel rollback de l'outer → divergence mémoire/disque.
   **Solution** : récupérer la fonction du target tool depuis le
   registry, l'appeler directement avec `(kg=, doc=, items=)`.
   L'outer Tx ouverte par le dispatcher couvre tout le batch
   atomiquement.

6. **Garde-fou `_is_many_tool`** : introspection de la signature du
   target — refus si pas de param `items`. Pas de hardcoded list de
   `*_many` tools à maintenir. Refuse aussi `llm_id` ou `items` dans
   `tool_args` (collision avec ce que `bulk_apply` construit).

7. **`bulk_resolve_filter` séparé** comme tool read-only pour preview.
   Permet au LLM de vérifier le périmètre avant de muter, et de
   répondre à des questions naturelles type « combien de fenêtres au
   N01 ». Tronque à 10 llm_ids par défaut + first/last + note.

8. **`change_param_bulk` non livré ce tour.** Hésitation : c'est un
   alias par-paramètre vers le `*_set_*_many` correspondant, mais
   ajoute une mapping `(param_name → tool_name)` couplée à la
   toolset. `apply_to_filter` couvre 100% des cas, juste un peu plus
   verbeux. Si la pratique LLM montre une friction, ajouter — sinon
   over-abstraction.

### Phase 1 — `lib/tools/bulk.py` (~250 lignes)

- **`_FILTER_KEYS`** : frozenset whitelist module-level.
- **`_validate_filter`** : refus de keys inconnues avec liste claire
  des keys autorisées.
- **`_match_node(attrs, filter)`** : AND-fold avec branches
  spécialisées pour `type` (compare `_type`), `name_contains`
  (case-insensitive substring), `name_regex` (regex avec
  ValueError sur pattern invalide), autres (comparaison directe).
- **`_resolve_filter(kg, filter) -> List[str]`** : combine validation +
  itération. Optimise via `find_by_type` quand `type` présent.
- **`_is_many_tool(entry)`** : introspection de la signature pour
  détecter `items` param. Pas de hardcoded list.
- **`bulk_resolve_filter`** (tier-1) : preview read-only, tronque les
  gros matchs.
- **`bulk_apply_to_filter`** (tier-1) : résout filter → si 0 match,
  no-op clair ; sinon construit items + dispatch direct → renvoie
  `{matched_count, target_tool, inner: <réponse *_many>}`.

### Phase 2 — Tests (+14)

`tests/test_tools.py` :

- **`resolve_filter`** (7 tests) : type seul, type+level_ref,
  filtre soft-deleted, name_contains case-insensitive, name_regex,
  refus key inconnue, truncation > preview_limit.
- **`apply_to_filter`** (6 tests) : no-match no-op, roundtrip
  walls_set_height_many succès, refus unknown target, refus non-many
  target, refus `llm_id` dans tool_args, atomic rollback sur inner
  failure (height_m négative).
- **Cas réel sim** : `bulk_apply_to_filter` sur Windows pour
  sill_height_m=0.80 — exactement le scénario soir 2026-05-11 session
  5 mais en filter-based.

### Validation

- `pytest -q` : **289 verts en 10.19s** (275 → +14). Aucune régression.
- Test live Revit : à coupler au prochain run, idéalement chaîné avec
  le scénario session b/c pour mesurer le gain (cible : 1 tool call
  au lieu de 2 — catalog + *_many).

### Couverture du gain ergonomie

Scénario « passe toutes les fenêtres du N01 à sill=0.80 » :

| Path | Round-trips | Tokens output LLM |
|---|---|---|
| Session 5 (avant `*_many`) | ~20 round-trips (1 par fenêtre) | ~600 |
| Session b (`*_many`) | 2 round-trips (catalog + many) | ~250 |
| Session e (filter-based) | **1 round-trip** | ~80 |

Session b a divisé par 10, session e divise encore par 3. Bénéfice
décroissant mais réel sur les workflows multi-bulk (20 setters_many
en série sur des filtres distincts).

### État final & reste à faire

**Acquis session e** :
- `bulk_resolve_filter` (tier-1, preview read-only) ✓
- `bulk_apply_to_filter` (tier-1, dispatch filter-based) ✓
- Garde-fous : keys whitelistées, validation target tool, refus
  `llm_id`/`items` dans tool_args ✓
- Dispatch direct sans nested transaction ✓
- 14 tests, baseline **289 verts** ✓
- Dette 1 session b (filter-based bulks) **réglée** ✓

**Dettes ouvertes (héritage)** :

- `change_param_bulk` — non livré. À évaluer après usage LLM réel
  (si le verbiage `apply_to_filter` cause friction).
- Drift utilisateur hors pipeline (events `DocumentChanged`).
- `boundary_walls` Rooms reporté V1 compliance.
- `connects_at` peuplé au rescan (préreq auto-cotation).
- `catalog_list_views` (préreq UC6 + cotation).

**Notes d'intention** (en attente) :
- Auto-cotation : préreqs identifiés, déclencheur = UC2/UC3 ou cas
  client.
- UC6 vision plan d'après image : préreqs identifiés, déclencheur =
  UC1 DWG livré ou cas client.
- Slash commands : déclencheur = ≥ 2 features cibles existantes.

**Suite immédiate (§9 V0 Sem.4-5)** :
- **UC1 DWG ingest** (`dwg_reader.py` + `dwg_classifier.py` + tools
  `dwg_import_*`) — le morceau restant de Sem.4-5. Pose la mécanique
  préprocesseur → wall segments → `walls_create_many` qui sera
  réutilisée pour UC6 raster. ~2-3 j homme estimés.

---

## 2026-05-12 — Note d'intention : plan d'après image (UC6 vision)

Conversation exploratoire utilisateur, **pas de code livré**. UC6 du
design (CLAUDE.md §Vision) explicitement reporté à V1. Cette note
capture l'architecture proposée et le pipeline de calibration
multi-indices pour ne pas refaire l'analyse à la reprise.

**Cas d'usage initial** : utilisateur fournit un plan poché raster
(ex : `plan_apartement_exemple.png`), demande « dessine les murs ».
Approche directe (LLM vision) → résultats mitigés. Diagnostic
partagé : un préprocesseur déterministe sur un plan poché (murs noirs
solides, géométrie orthogonale) est plus fiable qu'une interprétation
LLM. Pattern identique à UC1 (DWG ingest) et à UC8 compliance
(primitives déterministes + fallback LLM signalé).

### Pipeline cible (5 étapes, 4 déterministes)

1. **Threshold binaire** — Otsu auto ou seuil fixé (~25%).
2. **Nettoyage** — morphological opening pour rejeter bruit / fines
   hachures.
3. **Extraction contours** — `cv2.findContours` ou skimage.
4. **Approximation polygones orthogonaux** — `approxPolyDP` + axis-snap.
5. **Conversion pixel → mètres** — **vrai bloqueur**, voir calibration
   ci-dessous.

### Calibration multi-indices

Approche par **triangulation statistique** plutôt que référence unique.
Chaque détecteur émet un vote `ScaleEvidence(scale_m_per_px,
confidence, source, measurement_px, expected_m)`. Agrégation par
MAD outlier rejection + médiane pondérée par confidence.

**7 sub-détecteurs** par ordre de fiabilité native :

| Détecteur | Référence | Conf. native |
|---|---|---|
| Échelle graphique | motif `0—1—2—3 m` + OCR | 0.95 |
| Marches d'escalier | pas régulier 0.27-0.29 m, autocorrelation sur lignes parallèles fines | 0.85 |
| Cote dessinée | OCR tesseract sur nombres + mur adjacent | 0.85 |
| **Surface des pièces** | **OCR pattern `\d+([,.]\d+)?\s*m[²2]?` dans chaque cellule fermée → `scale = sqrt(area_m2 / area_px)`. Vote multi-pièces agrégé (N pièces → N votes croisés)** | **0.80** |
| Porte d'entrée | arc 90° rayon 0.90 m, `HoughCircles` | 0.75 |
| Portes internes | arc 90° rayon 0.80 m, vote agrégé sur N portes | 0.70 |
| Mobilier sanitaire / cuisine | WC 0.40 m, lavabo 0.55 m, plan travail 0.60 m | 0.55 |
| Épaisseur cloisons | 0.10-0.15 m, check de cohérence post-walls (pas primary) | 0.40 |

**Notes spécifiques au détecteur surface des pièces** :

- **Quasi-ubiquitaire** sur les plans d'habitation (architectes annotent
  presque toujours les m² par pièce) → ce détecteur a souvent N votes
  par plan, alors que cote dessinée n'en a typiquement que 1-2.
- **Dépendance 2e passe** : nécessite l'extraction des murs ET la
  reconstruction des cellules fermées (room detection topologique sur
  les segments murs) avant de pouvoir mesurer `area_px` par cellule.
  → s'exécute *après* `detectors/walls.py`, pas en parallèle.
- **Formulation `scale = sqrt(area_m² / area_px)`** plutôt qu'une
  longueur directe : la surface est un carré, donc l'erreur sur le
  scale est l'écart-type de la mesure / 2 (propagation). Avantage :
  l'aire est robuste aux distorsions locales (un mur légèrement
  imprécis ne décale pas la surface entière).
- **Aire = polygone réel, pas bbox** : pièces en L ou en T fréquentes
  en habitation → `cv2.contourArea` sur le contour de la cellule, pas
  `width × height` du bbox.
- **OCR caveat français** : `m²` est parfois exporté `m2` par tesseract
  (selon version + lang pack), virgule décimale standard FR (`12,5`).
  Regex tolérante : `r"(\d+)[,.]?(\d+)?\s*m\s*[²2]?"`.
- **Filtrage du bruit** : seuls les textes situés *à l'intérieur* d'une
  cellule fermée sont considérés (rejet des légendes, annotations
  hors-pièce). Robuste contre les "12 m²" qui apparaissent dans un
  bloc de texte général en marge.
- **Effet secondaire utile** : ce détecteur sert aussi de
  *post-validation* — une fois le scale calibré (par tout autre
  détecteur), recalculer les surfaces de toutes les pièces avec ce
  scale et comparer aux OCR. Match → confidence boost ; divergence
  systématique → l'estimation est probablement off-by-X%.

**Agrégation** :
1. Outlier rejection MAD (rejeter `|scale_i − median| > 3 × MAD`).
2. Médiane pondérée par confidence sur les votes restants.
3. Score de confiance global = `f(n_kept / n_total, spread,
   max_confidence_present)`. Présence d'un détecteur fort (échelle
   graphique / cote / escalier) → score plafond ≥ 0.8.
4. **User input traité comme prior fort, pas dur** : si l'utilisateur
   fournit une échelle (`confidence=1.0`) mais les détecteurs
   divergent fortement, le tool *signale* (« j'ai estimé 1:75 mais
   tu as donné 1:50 — vérifie ? ») plutôt que de subir muettement.
5. Demande à l'utilisateur si confidence < seuil (0.6 par défaut) avec
   un résumé des évidences pour qu'il puisse arbitrer en connaissance.

### Découplage pure-Python — décision

**Validé** : tout le pipeline est de l'analyse d'image, zéro
dépendance Revit. Donc développable / testable dans la `.venv/`
locale (CPython 3.13), pas besoin de pyRevit pour itérer.

Arborescence cible :

```
lib_floorplan/                 # standalone, zéro import Revit
├── preprocess.py              # threshold, denoise, morphology
├── detectors/
│   ├── walls.py               # contour → segments orthogonaux
│   ├── doors.py               # arcs HoughCircles
│   ├── stairs.py              # autocorrelation lignes parallèles
│   ├── scale_bar.py           # template + OCR
│   ├── dimension_text.py      # tesseract
│   ├── furniture.py           # template matching kitchen / sanitary
│   └── partition_thickness.py # check cohérence post-walls
├── scale_estimation.py        # MAD + weighted median + confidence
└── pipeline.py                # orchestrateur

claude-in-revit.extension/lib/tools/image_input.py   # ~50 lignes,
                                                      # wrapper tier-2
tests/
└── fixtures/
    ├── plan_apartement_exemple.png   # plan réel
    ├── plan_synthetic_50.png         # synthèse 1:50 ground truth
    └── plan_synthetic_100.png        # synthèse 1:100 ground truth
```

Bénéfices :
- Itération sans pyRevit (cycle dev rapide).
- Dataset fixture + ground truth → suite de tests qui valide la
  précision d'estimation. Régression auto à chaque tweak détecteur.
- Lib réutilisable hors projet (un script qui veut estimer une
  surface depuis un plan ne tire pas pyRevit).
- Dépendances lourdes (`opencv-python`, `scipy`, `tesseract`) isolées
  hors du runtime Revit qui reste maigre.

### Tool surface (Revit-side, après lib_floorplan livrée)

```python
@tool(name="image_extract_walls", tier=2)
def extract_walls(kg, doc, image_path, user_scale_hint=None):
    """Préprocesseur déterministe + calibration multi-indices.
    Renvoie segments, échelle estimée, score de confiance, évidences."""

@tool(name="image_draw_walls", tier=2)
def draw_walls(kg, doc, image_path, level_ref, wall_type_ref,
               user_scale_hint=None):
    """Orchestre extract_walls + walls_create_many."""
```

`tier=2` (chargé via `ROUTING_RULES` sur `image` / `plan` / `dessine
d'après`) — évite de polluer le catalogue par défaut.

### Limitations V1 phase 1 (assumées)

- Plans poché **orthogonaux** seulement (axis-aligned).
- Type unique (pas de distinction porteur / cloison à la création).
- Pas de détection automatique des ouvertures (portes / fenêtres
  ajoutées manuellement après création des murs, via les tools
  openings_create_* existants).
- Calibration multi-indices mais demande utilisateur en cas
  d'ambiguïté.

### Phases d'exécution (estimation)

| Phase | Livre | Effort |
|---|---|---|
| 0 — prérequis | UC1 DWG ingest (Sem.4-5 V0) — partage la mécanique préprocesseur → wall segments → walls_create_many | dans roadmap |
| 1a — lib_floorplan / walls | threshold + contours + segments orthogonaux + fixtures synthétiques | ~1 j |
| 1b — calibration multi-indices | 6 détecteurs + agrégateur MAD + confidence | ~2-3 j |
| 1c — wrapper Revit | image_extract_walls + image_draw_walls | ~½ j |
| 2 — détection ouvertures | arcs portes, double-trait fenêtres, retouche murs | ~2 j |
| 3 — porteurs vs cloisons | double seuil, distinction par épaisseur | ~2 j |
| 4 — non-orthogonal | post-processing Hough multi-angles | ~3-5 j |

Phase 1 (a+b+c) = ~4 j homme. Couvre 80% des cas typiques
(immeubles d'habitation, plans poché orthogonaux).

### Déclencheur de reprise

- UC1 DWG livré (Sem.4-5 V0) — la mécanique préprocesseur déterministe
  est éprouvée, on peut l'appliquer au raster.
- OU : cas client explicite (« je veux importer ce plan papier
  scanné »).

### Préreqs identifiés

- Aucun côté KG (Wall existe).
- Installation `opencv-python` ou `scikit-image` + `numpy` dans le
  CPython embarqué pyRevit (procédure CLAUDE.md déjà documentée).
- Optionnellement : `pytesseract` + binaires Tesseract pour OCR
  (cote dessinée + échelle graphique). Reportable à phase 1b.

---

## 2026-05-12 — Note d'intention : auto-cotation + slash commands

Conversation exploratoire utilisateur, **pas de code livré**. Consigne ici
les décisions de design et les préreqs identifiés pour ne pas refaire
l'analyse à la session suivante. Reprise : quand la roadmap §9 atteint
UC2/UC3 musclés ou quand le cas client réclame des cotations
automatiques.

### Auto-cotation — scope et préreqs

**Vision utilisateur** :
- Cotes externes générales (4 côtés du bbox), offset 1 m.
- Cotes internes par pièce.
- Règles graphiques : alignement horizontal / vertical autant que
  possible, pas de redondance (deux pièces juxtaposées de même largeur
  → cote uniquement la plus extérieure).

**3 difficultés majeures identifiées** :

1. **API Revit `Reference` notoirement fragile.** Obtenir un Reference
   stable depuis un mur multi-segments ou curtain demande de la
   gymnastique (`HostObjectUtils.GetSideFaces`,
   `FindReferencesByDirection`, etc.). C'est précisément ce que les
   plugins commerciaux encapsulent et facturent.

2. **L'algorithme de layout est où réside la valeur.** Décomposition
   des règles utilisateur :
   - Offset 1 m : paramètre trivial de `NewDimension`.
   - Alignement orthogonal : snap si angle mur < ε d'un axe.
   - Déduplication redondance : problème *graphe*. Deux approches :
     - **Topologique** via le KG (`connects_at` edges) — cohérent avec
       l'archi, mais nécessite de peupler `connects_at` au
       `full_rescan` (dette implicite).
     - **Géométrique** par clustering valeur + axe — plus simple, plus
       brittle sur plans non orthogonaux.
     Préférence : topologique (s'inscrit dans le KG, testable hors-Revit).

3. **Cotation intérieure dépend de `Room.boundary_walls`** — actuellement
   `[]` (reporté V0→V1, cf. session a 2026-05-12 décision 3). Donc
   phase « cotes intérieures » bloquée derrière cette dette.

**Décomposition en phases** (à exécuter dans l'ordre) :

| Phase | Livre | Bloquants amont | Effort |
|---|---|---|---|
| 0 — préreqs | Peupler `connects_at` au rescan + `Room.boundary_walls` calculé via `GetBoundarySegments` + `catalog_list_views` | aucun, bénéfique indépendamment | ~½ jour |
| 1 — externes seules | `dimensions_create_external(view_ref, offset_m=1.0)` : bbox + 4 chaînes + dédup horiz/vert | `catalog_list_views` (phase 0 partiel) | ~1 jour |
| 2 — internes | `dimensions_create_room_interior(room_ref, offset_m)` + dédup inter-room | phase 0 complet (boundary_walls) | ~1-2 jours |
| 3 — orchestrateur | `dimensions_auto_all(view_ref)` + `dimensions_clear` + `dimensions_purge_redundant` | phases 1+2 | ~½ jour |

**Architecture** :
- `lib/dimensioning_strategy.py` — pure logic (no Revit imports),
  testable hors-Revit. Calcule les chaînes de cotes à poser à partir
  d'une représentation abstraite des murs / refs / pièces.
- `lib/tools/dimensioning.py` — wrappers tools, doc-aware comme
  partout.
- `revit_primitives.py` — extensions Reference handling
  (`wall_face_references(wall, side)`, `room_boundary_references(room)`).
- Schéma KG : décision à prendre — node type `Dimension` ou
  annotations purement view-side hors KG ? Argument pour le KG :
  permet la dédup et la "Refresh dim" symétrique au reste. Argument
  contre : annotations sont view-bound, pas modèle, pollution du KG.
  À trancher au moment de l'implémentation.

**Estimation totale** : 3-4 jours homme. Significatif mais
décomposable. Pas dans le scope V0 (§9 — Sem.4-5 = DWG + bulk
génériques).

**Déclencheur de reprise** : quand un client demande explicitement de
l'auto-cotation, ou quand UC2/UC3 (modifications géométriques
musclées) deviennent prioritaires et qu'on veut le pipeline complet
« crée → cote → exporte ».

### Slash commands — design retenu

**Contrainte design** : CLAUDE.md §Vision verrouille un *unique point
d'entrée conversationnel* (`prompt.pushbutton`). Pas de pushbutton
par feature. Donc on reste dans la zone de texte avec du sucre
syntaxique côté script.

**Option retenue** : **A + D combinés** (voir réponse utilisateur
in-conversation pour les 4 options évaluées) :

- **D** (passive, déjà en place de facto) : le LLM route correctement
  les phrases naturelles (« cote tout », « auto-cote ce plan ») vers
  les bons tools via les `Phrases:` des docstrings. Pas de code à
  écrire — c'est l'effet du registry tier-1 + routing.
- **A** (active, à implémenter quand utile) : parser dans
  `prompt.pushbutton/script.py` — `/<word>` en début de message est
  remplacé par un prompt long pré-rédigé chargé depuis
  `~/.config/claude-in-revit/slash_commands.json`. ~30 LoC.

**Mapping initial** (à étendre au fil des features) :
```json
{
  "/auto-cotation": "Génère les cotations auto sur la vue active : externes 1m offset puis intérieures par pièce, supprime redondances. Utilise dimensions_auto_all.",
  "/audit-walls": "Compte les murs par niveau et par type, signale les anomalies (hauteurs incohérentes, types orphelins).",
  "/refresh-rooms": "Lance rooms_recompute_boundaries sur toutes les pièces et rapporte les aires."
}
```

**Tool optionnel** : `/help` qui liste les commandes disponibles
(pratique pour la découverte).

**Déclencheur de reprise** : quand au moins **2 features cibles
existent** dont la commande aurait du sens (auto-cotation + une
autre). Implémenter A pour un seul mapping est over-engineering.

### Préreqs identifiés pour la suite (résumé exécutable)

Ces 3 dettes sont **bénéfiques indépendamment** de l'auto-cotation :

1. **Peupler `connects_at`** dans `kg_sync.full_rescan` — détection des
   coins / T / cross entre murs. Le edge_type existe dans
   `EDGE_TYPES` mais n'est jamais posé. Utile dès qu'on veut raisonner
   sur l'enveloppe d'un bâtiment.

2. **Calculer `Room.boundary_walls`** via `Room.GetBoundarySegments`
   au `_room_to_attrs`. Permet aussi UC8 compliance (parcours
   d'évacuation, hauteur sous plafond par room avec mur adjacent).

3. **`catalog_list_views`** — Plan, Section, 3D. Inventaire des vues
   du projet. Utile pour tout tool view-bound (annotations,
   dimensions, tags, exports).

Reprises naturelles : au prochain run où l'un de ces 3 manque,
prioriser celui qui débloque le tool en cours plutôt que de tout
faire d'un bloc.

---

## 2026-05-12 (session d) — `openings_purge_unused_variants`

### Contexte & objectif

Dette de la session c (point 1 « Pollution du browser Revit sur le long
terme ») : l'auto-découple crée des FamilyTypes `[auto h<NN>cm]` qui
s'accumulent. Idempotence (variants réutilisés) limite déjà l'explosion,
mais après des cycles de modifications variées (sill 0.6, sill 0.8,
head 2.0, head 2.4…) on finit avec une famille qui a 5-10 variants
auto, dont certains plus utilisés. Tool de maintenance ciblé pour
nettoyer.

### Décisions

1. **Détection par marqueur de nom `[auto h<NN>cm]`** (regex). Cohérent
   avec la convention de la session c. Conservateur : un variant renommé
   par l'utilisateur (marqueur enlevé) est *préservé* — le renommage
   signale une réappropriation du variant comme type normal.

2. **Pas de tag explicite `_auto_generated: True`.** Option envisagée
   puis écartée : nécessiterait une extension de schéma sur `FamilyType`,
   et le marqueur de nom suffit. Si l'utilisateur enlève le marqueur
   pour éviter la purge, c'est un signal volontaire — pas un bug.

3. **Soft-delete côté KG, hard-delete côté Revit.** Symétrique aux
   autres `*_delete`. Le node FamilyType reste tracé avec
   `deleted_at_turn` (audits, lineage). Le FamilySymbol Revit disparaît.

4. **Filtre catégorie optionnel.** `category="Doors"` / `"Windows"` /
   `None` (défaut = tout). Utile si l'utilisateur veut purger
   sélectivement (« nettoie seulement les variants de fenêtres »).

5. **Vérification d'usage avant suppression.** `_is_family_type_in_use`
   itère les Door/Window vivants et matche `type_ref`. Variants
   référencés uniquement par des openings soft-deleted = unused
   (`find_by_type` filtre déjà les soft-deleted). Évite un
   `doc.Delete` qui ferait crasher si Revit avait des refs cachées.

6. **Tolérance à un refus Revit.** Si `doc.Delete` lève (rare : usage
   par un élément hors KG, lock, etc.), on ajoute l'item dans `kept`
   avec `reason: "revit_refused_delete: <exc>"`. Le batch continue.

7. **Pas de dry-run en V0.** Tentation : flag `dry_run: bool = False`
   pour preview. Reporté — l'utilisateur peut toujours appeler le
   tool, voir `purged` dans la réponse, et undo Revit si nécessaire
   (le `doc.Delete` est dans une Tx Revit, donc Ctrl+Z annule). Si
   le cas devient critique, ajout trivial.

### Phase 1 — Helpers privés

`lib/tools/openings.py` :

- **`_AUTO_VARIANT_MARKER_RE = re.compile(r"\[auto h\d+cm\]")`**.
  Module-level constante. Match suffixe, tolérant aux modifications
  utilisateur tant qu'elles préservent le marqueur.
- **`_is_auto_variant(node) -> bool`** : check `_type == "FamilyType"`
  + regex search sur `type_name`.
- **`_is_family_type_in_use(kg, family_type_ref) -> bool`** : itère
  `find_by_type("Door")` + `("Window")` (filtre soft-deleted
  automatique), compare `type_ref`.

### Phase 2 — Tool `openings_purge_unused_variants`

Signature : `(kg, doc, category=None) -> Dict`. Logique en 3 étapes :

1. Collecte les FamilyType auto, filtre par catégorie si demandée.
2. Sépare unused vs in_use (`_is_family_type_in_use` per candidat).
3. Pour chaque unused : `doc.Delete` (Revit) + `kg.soft_delete` (KG).

Réponse compacte : `{ok, scanned, purged, kept, revit_deleted}`. `kept`
n'enumère QUE les variants conservés pour usage (token compact —
typiquement [] sur un projet fraîchement purgé). Refusé : `category`
non valide → `ValueError` claire (`"category must be 'Doors',
'Windows' or None"`).

### Phase 3 — Tests (+6)

`tests/test_tools.py`, basés sur la fixture `kg_with_window_with_rigid_type`
(session c) :

1. **`test_purge_unused_variants_drops_orphans_keeps_used`** : 2 auto-variants,
   1 utilisé par w1, 1 orphelin. Purge → scanned=2, purged=1,
   utilisé apparaît dans kept avec reason=in_use.
2. **`test_purge_unused_variants_ignores_non_auto_types`** : un
   FamilyType normal sans marqueur, orphelin → scanned=0 (pas
   touché).
3. **`test_purge_unused_variants_filter_by_category`** : 2 orphans
   un Windows + un Doors, category="Windows" → seul le Windows
   purgé.
4. **`test_purge_unused_variants_treats_soft_deleted_openings_as_unused`** :
   variant dont la seule fenêtre référente est soft-deleted → purgé.
5. **`test_purge_unused_variants_no_auto_types_present`** : projet
   vierge → scanned=0, purged=0, kept=[].
6. **`test_purge_unused_variants_refuses_invalid_category`** :
   category="Walls" → erreur explicite.

### Validation

- `pytest -q` : **275 verts en 8.99s** (269 → +6). Aucune régression.
- Test live Revit : à coupler avec le scénario de la session c
  (head=2m × 20 fenêtres mixtes) + un appel `purge_unused_variants`
  en fin pour observer le ménage. Critère : variants `[auto]`
  effectivement supprimés du browser Revit.

### État final & reste à faire

**Acquis session d** :
- `openings_purge_unused_variants` tool ✓
- Détection conservatrice par marqueur de nom ✓
- Filtre catégorie ✓
- Symétrie KG / Revit (soft / hard) ✓
- Tolérance refus Revit ✓
- 6 tests, baseline **275 verts** ✓

**Dettes / TODO ouverts** :
- Dry-run reporté (Ctrl+Z Revit fait office).
- Cas Top Constraint murs (analogie sill/head, prévention préemptive)
  toujours ouvert. Pas un blocker tant qu'aucun cas réel ne remonte.
- Drift utilisateur hors pipeline (events `DocumentChanged`) toujours
  ouvert.

**Boucle vertueuse** : auto-découple (session c) ajoute des variants
proprement nommés, purge (session d) les nettoie quand orphelins.
L'utilisateur peut maintenant modifier sill/head librement sans gérer
manuellement les FamilyTypes — c'était l'invariant attendu (« je
pensais que ce comportement était acquis »).

**Suite immédiate (§9 V0 Sem.4-5)** : inchangée — `dwg_reader.py` +
`tools/bulk.py`.

---

## 2026-05-12 (session c) — Auto-découple sill ↔ head dans les setters d'openings

### Contexte & objectif

Bug report utilisateur : « passe la hauteur sous linteau à 2 m » sur
une sélection de fenêtres (sill=1.0, head=2.2, family `opening_height=1.2`)
a produit head=2.0 ET sill=0.8 — Revit a recomputé l'allège pour
préserver `opening_height = head − sill = 1.2`. Comportement attendu
côté utilisateur : « il faut découpler les deux et créer un nouveau
type si nécessaire. je pensais que ce comportement était acquis ».

Diagnostic : sessions 5 + b livraient un canal *réactif* (drift_note
post-mortem dans `tool_result`) mais aucun pré-flight *préemptif* — le
LLM voyait le drift après que la mutation avait déjà eu lieu, et en
pratique ne corrigeait pas systématiquement (et même s'il le faisait,
l'utilisateur avait déjà subi la mutation parasite). La donnée pour
prédire était pourtant *déjà dans le KG* (`FamilyType.dimensions.height_m`
peuplé par `_family_type_to_attrs` au rescan, session 4).

L'invariant KG = Revit *post-mutation* tient grâce à la discipline
read-back. Mais l'invariant utilisateur = Revit *post-mutation* nécessite
en plus un pré-flight qui empêche les mutations parasites de se
produire. C'est cette deuxième couche qui manquait.

### Décisions

1. **Auto-découple par défaut (option A retenue après échange UX).**
   `set_sill_height` / `set_head_height` (+ `_many`) préservent par
   défaut l'autre dimension. Le pré-flight bascule sur un variant de
   type compatible si la cible diverge de la `opening_height` familiale.
   Escape hatch : `preserve_head=False` / `preserve_sill=False`.

2. **Recherche-puis-création (idempotence).** Avant de créer un
   variant, le helper `_find_compatible_variant` cherche dans le KG un
   FamilyType de la même famille + catégorie dont
   `dimensions.height_m` matche la cible (`5e-4 m` près). Si trouvé,
   swap vers lui. Sinon, création + swap. **N appels successifs avec
   la même cible réutilisent le même variant** — pas d'explosion du
   browser Revit.

3. **Convention de nommage `<type> [auto h<NN>cm]`** (option choisie
   par l'utilisateur). Marqueur `[auto]` rend le variant identifiable
   dans le browser ; hauteur en cm pour la concision. Idempotent par
   construction : pour une même cible, le nom est déterministe.

4. **Fallback gracieux** quand `dimensions.height_m` est absent.
   Familles non-paramétrées (ou dont le cascade `WINDOW_HEIGHT` /
   `DOOR_HEIGHT` / `LookupParameter` a échoué au rescan) → pas de
   prédiction possible → `decoupled=False`, Set direct, drift signalé
   au post-mortem comme avant. Pas de blocage, pas d'exception.

5. **Helpers privés extraits** plutôt que d'appeler les tools via
   dispatch. `_create_type_variant_internal` et `_swap_to_type_internal`
   peuvent être appelés depuis l'intérieur d'une transaction Revit
   ouverte par le setter, alors que les tools `openings_create_type_variant`
   et `openings_set_type` ouvrent leur propre transaction (incompatible
   avec une Tx en cours). Pas de duplication de logique — les tools
   originaux délèguent aussi aux helpers (à terme).

6. **Compteurs agrégés dans les `_many`** : `decoupled_count` et
   `auto_variants_created` dans la réponse en plus de
   `bulk_setter_summary`. Le LLM voit en un coup d'œil que sur 20
   fenêtres, 20 ont été découplées et 1 seul variant créé (les 19
   autres l'ont réutilisé).

7. **`set_type` (solo + many) volontairement non touchés.** Ces tools
   ont une sémantique propre — l'utilisateur les utilise quand il veut
   *explicitement* changer de type, et la question du découple ne se
   pose pas (c'est ce qu'il fait, par définition).

8. **Stratégie drift confirmée** (cf. réponse au cours de la session
   b) : tolerance + early exit + zero-token-for-clean + no-re-echo
   restent les leviers. L'auto-découple ne *remplace* pas la discipline
   read-back, il l'augmente d'une couche préemptive — la défense reste
   en profondeur.

### Phase 1 — Helpers privés (lib/tools/openings.py)

Ajoutés (~200 lignes) après `_DRIFT_EPSILON_M`, avant les setters
solo :

- **`_create_type_variant_internal(kg, doc, *, source_type_ref, new_name,
  opening_height_m, opening_width_m=None) -> str`** : duplication
  KG / Revit, write opening_height via la cascade
  `rp.opening_set_height`, lecture post-Set des dimensions, retour du
  nouveau llm_id. Aucune `rp.transaction` ouverte ici — le caller est
  *déjà* dans la sienne.

- **`_find_compatible_variant(kg, *, source_type_ref,
  target_opening_height_m) -> Optional[str]`** : itère
  `kg.find_by_type("FamilyType")`, filtre par `family_name` +
  `category`, compare `dimensions.height_m` à la cible (tolérance
  `_DRIFT_EPSILON_M`). O(N) sur le nombre de FamilyType — typiquement
  <50, donc négligeable.

- **`_variant_name(source_type_name, opening_height_m) -> str`** :
  format `<src> [auto h<NN>cm]`. `round(h*100)` pour les centimètres.

- **`_swap_to_type_internal(kg, doc, llm_id, new_family_type_ref) -> Dict`** :
  swap `Symbol` + reroute edge `is_type` + lecture sill/head post-swap.
  Symétrie KG-only / Revit.

- **`_maybe_decouple(kg, doc, llm_id, *, new_head_m=None,
  new_sill_m=None) -> Dict`** : orchestre les 4 helpers ci-dessus.
  Renvoie `{decoupled, new_type_ref, auto_variant_created}`. Validation :
  exactement un de `new_head_m` / `new_sill_m` doit être fourni.

### Phase 2 — Patch des 4 setters

Modifications minimales par tool :

- **Signature** : ajout du flag `preserve_head=True` /
  `preserve_sill=True` (défaut auto-découple).
- **Corps** : appel à `_maybe_decouple` *avant* le path KG-only ou Revit
  quand le flag est True. Re-lecture de `node` après swap potentiel
  (sill/head ont pu changer post-swap, l'écriture explicite qui suit
  réaligne le param visé).
- **Retour** : merge de `decouple_info` (`decoupled`, `new_type_ref`,
  `auto_variant_created`) dans le dict de réponse via `**decouple_info`.
- **Docstring** : section dédiée « Auto-découple sill ↔ head », mention
  de l'escape hatch.

`_many` variants : compteurs agrégés (`decoupled_count`,
`auto_variants_created`) ajoutés post-`bulk_setter_summary`.

### Phase 3 — Tests (+8)

`tests/test_tools.py` :

- Fixture dédiée `kg_with_window_with_rigid_type` : 2 fenêtres + 1
  FamilyType avec `dimensions.height_m=1.2` (= contrainte familiale
  rigide). État initial cohérent : sill=1.0, head=2.2.
- **`test_set_head_height_auto_decouples_creates_variant`** : cible
  head=2.0 → target_opening=1.0 ≠ 1.2 → découple, variant créé,
  sill préservé à 1.0.
- **`test_set_sill_height_auto_decouples_creates_variant`** : symétrique
  côté sill.
- **`test_set_head_height_reuses_existing_variant`** : 2 appels
  successifs sur sibling, le second réutilise (auto_variant_created=False).
- **`test_set_head_height_no_decouple_when_target_matches_family`** :
  no-op réel (cible=état courant) → pas de découple.
- **`test_set_head_height_no_decouple_when_family_has_no_dimensions`** :
  FamilyType sans dimensions → fallback legacy.
- **`test_set_head_height_preserve_sill_false_bypasses`** : escape
  hatch.
- **`test_set_head_height_many_aggregates_decouple_counters`** : 3
  fenêtres, decoupled_count=3, auto_variants_created=1 (1 créé,
  2 réutilisés).
- **`test_set_sill_height_many_preserve_head_false`** : escape hatch
  côté `_many`.

### Validation

- `pytest -q` (suite complète) : **269 verts en 8.46s** (261 → +8).
- Aucune régression sur les 261 tests existants.
- Validation runtime Revit : non tentée ce tour (la mécanique est
  unit-testable end-to-end côté KG, les helpers Revit-side sont des
  re-uses des chemins éprouvés `Duplicate` + `Symbol assignment` des
  sessions 4 / b). À couvrir à la prochaine session Revit avec
  exactement le scénario rapporté par l'utilisateur (« head=2m sur
  fenêtres sélectionnées »).

### Couverture du bug rapporté

Avant : `set_head_height(w, 2.0)` sur fenêtre sill=1.0 head=2.2 avec
family h=1.2 → Revit committe sill=0.8 head=2.0 (recompute parasite).
KG mirror la réalité (session 5 read-back), `drift_note` posée — mais
l'utilisateur subit la mutation.

Après : même appel → pré-flight calcule target_opening=1.0, détecte
divergence avec family.h=1.2, cherche un variant compatible, n'en
trouve pas, crée `<src> [auto h100cm]` avec opening_height=1.0,
swap, *puis* setter le head=2.0. Résultat Revit : sill=1.0 (préservé)
+ head=2.0. Réponse au LLM signale `decoupled=True,
auto_variant_created=True, new_type_ref=<id>` → l'utilisateur voit
explicitement qu'un variant a été créé.

### État final & reste à faire

**Acquis session c** :
- 4 setters d'openings auto-découplent par défaut ✓
- Helpers privés réutilisables (`_create_type_variant_internal`,
  `_find_compatible_variant`, `_swap_to_type_internal`,
  `_maybe_decouple`) ✓
- Idempotence (variants réutilisés entre appels) ✓
- Convention `<type> [auto h<NN>cm]` ✓
- Escape hatch documenté ✓
- 8 tests KG-only, baseline **269 verts** ✓
- Bug utilisateur (head=2m → sill=0.8 parasite) **réglé** ✓

**Dettes ouvertes héritage** :
- Filter-based bulks reporté à `tools/bulk.py` Sem.4-5.
- Drift utilisateur hors pipeline (events `DocumentChanged`).
- `boundary_walls` Rooms reporté pour compliance V1.

**Nouvelles considérations** :

1. **Pollution du browser Revit** sur le long terme. Même avec
   idempotence, un projet où l'utilisateur change souvent les
   sill/head finit par accumuler des variants `[auto h<NN>cm]`. Pas
   un blocker (les variants restent groupés par famille, lisibles),
   mais un nettoyage périodique peut s'avérer utile. Tool potentiel :
   `openings_purge_unused_variants` qui supprime les FamilyType avec
   préfixe `[auto]` qui n'ont plus d'instance les utilisant. Reporté.

2. **Test live `head=2m sur 20 fenêtres mixtes`** : à reproduire avec
   l'auto-découple actif. Attendu : ~3 round-trips (1 catalog + 1 set
   par catégorie de type unique) au lieu de ~44 (session 5
   pré-setters_many) ou ~6 (session b sans auto-découple). Critère
   d'acceptation : sill préservé sur les 20, autant de variants
   `[auto h100cm]` créés que de familles distinctes dans la sélection.

3. **Ne couvre PAS le cas Top Constraint sur les murs** — analogue
   au sill/head : Top Constraint sur un mur fige la hauteur ; un
   `set_height` sur ce mur va dériver. La discipline read-back +
   drift_note l'attrape post-mortem, mais aucune auto-correction
   préemptive. À traiter si le cas devient courant — note pour le
   futur.

**Suite immédiate (§9 V0 Sem.4-5)** : inchangée — `dwg_reader.py` +
`tools/bulk.py`. La dette session 5 est désormais réglée tant en
*couverture bulk* (session b) qu'en *prévention drift* (session c).

---

## 2026-05-12 (session b) — Setters multi-objets (dette 4 session 5)

### Contexte & objectif

Dette héritée session 5 (2026-05-11, point 4 du « Reste à faire ») :
le scénario soir « passe toutes les fenêtres à sill=0.80, head=2.20 »
consommait ~44 tool_use blocks pour 20 fenêtres × types mixtes. Cible
mesurée : ~2 tool calls si on dispose de `*_many` setters côté KG +
Revit. Avec la surface mutante complète depuis la session a (rooms +
levels écriture), c'est le moment de la livrer.

Discussion utilisateur en cours de session sur la **stratégie de coût
minimum du drift check** — réponse consignée ici pour mémoire (cf.
§« Stratégie drift » plus bas).

### Décisions

1. **Items-based plutôt que filter-based.** Filter-based (« passe
   toutes les windows du N01 à sill=X » résolu côté KG) reporté à
   `tools/bulk.py` (§9 V0 Sem.4-5). Raisons :
   - Items-based est explicite : le LLM construit la liste depuis
     `catalog_list_*` (déjà cachée typiquement), pas d'ambiguïté sur
     le périmètre filtré.
   - Filter-based a des coins subtils (inclure/exclure soft-deleted,
     ordre des filtres, intersection multi-fields). Vaut mieux le
     traiter une fois pour toutes dans un module dédié plutôt qu'en
     surcouche par paire de tools.
   - Bénéfice tokens : items-based réclame une liste avec llm_id ×
     N (≈10 tokens/item × 20 = 200 tokens). Filter ferait ~30 tokens.
     Différence acceptable face au gain de clarté.

2. **Réponse compacte `bulk_setter_summary`** distincte de
   `bulk_summary` (qui sert pour `*_create_many`). Shape :
   `{ok, count, drifted_count, drifts: [{llm_id, note}, …],
     revit_modified}`. Les items committés clean *n'apparaissent pas*
   dans `drifts` — token cost en `O(K)` avec K = drifts (typiquement
   0), pas `O(N)`. Helper centralisé dans `_helpers.py`.

3. **Validation upfront atomique.** `[_validate_*_item(kg, it, i) for
   i, it in enumerate(items)]` avant *toute* mutation. Si un seul item
   est invalide → `ValueError` remontée → dispatcher rollback la
   snapshot KG. Pas de demi-batch.

4. **Symétrie sill ↔ head préservée dans les bulks.** `set_sill_height_many`
   relit sill ET head après chaque `param.Set` (idem session 5). Le
   `drift_note` pointe vers `openings_set_type_many` /
   `openings_create_type_variant` quand la contrainte familiale impose
   un recompute.

5. **`set_type_many` groupe l'activation par symbole.** Si N items
   visent le même `new_family_type_ref`, on `Activate() + Regenerate()`
   une seule fois (set `activated: set`). Le coût Revit du
   `Regenerate` n'est pas négligeable sur de gros bulks.

6. **`rooms_set_name_many` SANS pré-check de collision.** Revit
   autorise plusieurs rooms homonymes (c'est `Number` qui est unique,
   pas `Name`). Comportement délibéré, testé. Contraste avec
   `levels_set_name_many` qui n'existe pas — si on le livrait il
   devrait faire un pré-check global (Levels exigent name unique).

7. **`levels_*_many` non livrés.** Cas d'usage extrêmement rare —
   personne ne bulk-modifie des Levels en pratique. Documenté comme
   omission consciente, pas un oubli. Si une session future en a
   besoin, c'est 30 minutes de plus.

### Phase 1 — `bulk_setter_summary` dans `_helpers.py`

Helper symétrique à `bulk_summary` (10 lignes). Reçoit la liste des
drifts + le count total + le flag `revit_modified`. Le caller construit
les drifts au fur et à mesure de sa boucle, sans avoir à connaître la
shape de réponse — le contrat est centralisé.

### Phase 2 — `walls.py` : `_set_height_many`, `_move_many`

Validators dédiés (`_validate_set_height_item`, `_validate_move_item`)
qui poppent un `ValueError` riche en contexte (`"items[3]: ..."`).
Pattern uniforme :
1. Validation upfront atomique (tous les items ou rien).
2. Bindings pré-check avant tout import Revit.
3. Une seule `rp.transaction(doc, "<name>")` enveloppe la boucle.
4. Read-back per-item via `refresh_node_from_revit`, drift collecté.

`walls_move_many` calcule `requested_p1/p2` *avant* `MoveElement` (en
ajoutant dx/dy à `p1/p2` courants du KG), parce qu'après
`refresh_node_from_revit` le KG est déjà à jour avec la valeur
Revit-committée — on perdrait le "what we asked" sinon.

### Phase 3 — `openings.py` : `_set_sill_height_many`, `_set_head_height_many`, `_set_type_many`

- `_set_sill_height_many` et `_set_head_height_many` : pattern uniforme
  via `_validate_sill_or_head_item(field=...)` paramétré sur le champ
  visé. Relit `(sill, head)` après chaque Set (réutilise `_read_sill_head_m`
  et `_drift_note`, sans duplication).
- `_set_type_many` : `_validate_set_type_item` impose la match catégorie
  (Door ↔ Doors, Window ↔ Windows) — pas de cross-category swap par
  accident. Active chaque nouveau FamilySymbol une seule fois par
  batch. `drifts=[]` toujours retourné : le swap est binaire (il a lieu
  ou il échoue), pas un drift au sens « sill/head decalé » — ces
  derniers apparaîtraient au prochain `_set_sill_height_many` chained.

### Phase 4 — `rooms.py` : `_set_name_many`

Pas de pré-check collision (cf. décision 6). Le test
`test_rooms_set_name_many_allows_duplicate_names` documente le
comportement attendu.

### Phase 5 — Tests + registry expected set

- `test_canonical_registry_has_expected_tier1_tools` étendu de 6
  nouvelles entrées.
- 16 nouveaux tests fonctionnels :
  - 2 tests `bulk_setter_summary` (shape vide, shape avec drifts).
  - 4 tests walls (`set_height_many` succès, atomique, refus empty,
    refus non-Wall ; `move_many` succès, atomique).
  - 5 tests openings (`set_sill_height_many` succès, refus non-opening ;
    `set_head_height_many` succès ; `set_type_many` succès,
    refus category mismatch).
  - 4 tests rooms (`set_name_many` succès, accepte doublons, atomique).

Tous KG-only — pas de stub Revit nécessaire. La branche `doc is None`
est exercée pour la mécanique d'agrégation et la validation. Le chemin
Revit (transactions imbriquées, drift réel) sera couvert au test live
de la prochaine session.

### Stratégie drift — réponse à la question utilisateur

Question : peut-on imaginer un mécanisme hash-based (style SHA) pour
court-circuiter la détection de drift en `O(1)` ?

Décision : non, et le coût actuel est déjà le minimum sous contrainte
de correctness. Argumentaire en 4 leviers :

1. **Early exit tolérance** : `detect_drift` retourne `(False, None)`
   immédiatement si `|requested − committed| ≤ 5e-4 m`. Aucune
   allocation, aucun formatage. ~3 ops CPU par item.
2. **Token zéro pour clean** : seuls les items ayant drifté entrent
   dans `drifts`. Common path → ~50 tokens quel que soit N. Cost en
   `O(K)` (drifted), pas `O(N)`.
3. **Read-back unitaire mais bon marché** : `GetElement(eid) +
   AsDouble()` → O(1) sur la table Revit. Inévitable pour la
   correctness (cf. session 5), mais ne domine pas le profil.
4. **Pas de re-echo des valeurs** : la réponse ne contient pas la
   liste `[{llm_id, requested, committed}, ...]` pour les N items —
   le LLM requery via `catalog_list_*` si besoin (rare). Économie
   tokens majeure sur les bulks larges.

Pourquoi le hash n'aiderait pas :
- L'état par item est minuscule (1-2 floats ou une string) — hasher
  coûte plus que comparer directement.
- Un hash global collapse N drifts en 1 booléen, mais on *veut*
  savoir lesquels (pour le `drift_note`). Re-enumeration derrière.
- Pas de comparaison répétée à amortir.

Optimisations différées si profilage le réclame :
- **Agrégation drift_note motifs identiques** (12 windows même
  conflit → 1 note groupée). Bénéfice tokens, pas latence.
- **Cache famille-rigide-connue** : court-circuiter le read-back
  sur familles toujours-driftantes. Brittle, à éviter sauf evidence.

### Validation

- `pytest -q` : **261 verts en 8.79s** (245 → +16).
- Aucune régression.
- Pas de validation runtime Revit ce tour : tools KG-only-testable,
  plomberie Revit isolée et déjà éprouvée par les setters solo (session
  5). Test live à coupler au prochain run, idéalement le scénario
  20-fenêtres pour confirmer le gain ~44 → ~2 tool calls.

### État final & reste à faire

**Acquis session b** :
- `bulk_setter_summary` helper (shape compact `O(K)` tokens) ✓
- `walls_set_height_many`, `walls_move_many` ✓
- `openings_set_sill_height_many`, `_head_height_many`, `_set_type_many` ✓
- `rooms_set_name_many` ✓
- Validation atomique upfront, single Tx Revit + KG par batch ✓
- Discipline read-back + drift signaling préservée ✓
- 16 tests, baseline **261 verts** ✓
- Dette 4 session 5 **réglée** (items-based) — gain estimé ~44 → ~2
  tool calls sur le scénario 20 fenêtres soir 2026-05-11.

**Dettes / TODO ouverts** :

1. **Filter-based bulks** (reporté à `tools/bulk.py` Sem.4-5). Cas
   d'usage : « passe toutes les fenêtres du N01 à sill=0.80 » résolu
   côté KG sans que le LLM ait à enumerer les llm_ids. Token saving
   marginal vs items-based (~150 tokens), bénéfice principal =
   ergonomie. À traiter avec UC7 (modifications en masse).
2. **`levels_*_many` non livrés** (cf. décision 7). Rejoindre si
   besoin se manifeste.
3. **Drift utilisateur hors pipeline** (trou architectural connu) —
   utilisateur édite un mur dans l'UI Revit, KG ne sait pas. Aujourd'hui
   bouton `refresh_kg` manuel uniquement. Solution propre : abonner
   aux events `DocumentChanged` de Revit. Reporté.
4. **Test live 20-fenêtres** pour confirmer le gain mesuré. À couvrir
   à la prochaine session Revit.

**Suite immédiate (§9 V0 Sem.4-5)** :
- `dwg_reader.py` + `dwg_classifier.py` (ezdxf) → UC1.
- `tools/bulk.py` (`apply_to_filter`, `change_param_bulk`) → UC7 +
  couvre la dette 1 ci-dessus.

---

## 2026-05-12 — Rooms + Levels (écriture) — clôture V0 Sem.2-3

### Contexte & objectif

Reprise après la session 5 du 2026-05-11 (228 tests, discipline read-back
sur 14 tools mutants). Reste à boucler §9 Sem.2-3 V0 : `rooms.py` (create,
recompute_boundaries, set_name, get_area) et `levels.py` côté écriture
(create, modify_elevation, set_active). Une fois bouclé, la surface
géométrique de base est complète et UC2/UC3 deviennent pleinement
fonctionnels. Ouvre aussi la voie compliance (UC8, §4.5) qui s'appuie sur
`Room.use_subcategory` + `ProjectContext`.

### Décisions

1. **`set_active` (Level) omis pour V0.** C'est une opération UX sur les
   vues (changer le plan d'étage actif dans l'UIDocument), pas une
   mutation du modèle. L'utilisateur peut basculer de vue directement
   dans Revit. Documenté en tête de `levels.py`.

2. **`levels_delete` également omis.** Supprimer un niveau casse les
   refs `at_level` de tous les éléments hôtés (Walls, Columns, Rooms,
   Doors, Windows) ; une stratégie de re-hosting / soft-cascade n'est
   pas dans le scope de la session. Reporté.

3. **`boundary_walls` resté à `[]` en V0.** Le calcul réel via
   `Room.GetBoundarySegments` (qui retourne `IList<IList<BoundarySegment>>`)
   nécessite de matcher chaque `seg.ElementId` contre le KG et de gérer
   les segments non-wall (room separation lines). Brittle sans projet
   Revit pour valider. Reporté à la compliance UC8 (§4.5) où la liste
   devient load-bearing (audit hauteur sous plafond par room, parcours
   d'évacuation, etc.).

4. **`rooms_recompute_boundaries` plutôt que `rooms_refresh_areas`.**
   Le tool fait deux choses sous le capot : `doc.Regenerate()` qui
   force Revit à recalculer les loops de toutes les rooms, *puis*
   `refresh_node_from_revit` qui mirror l'aire post-régen dans le KG.
   Le nom du tool reflète la sémantique côté Revit (recompute) plutôt
   que côté KG (refresh) — c'est ce que le LLM doit appeler quand
   l'utilisateur ferme des murs après une création.

5. **`rooms_get_area` lit le KG par défaut, refresh si `doc` présent.**
   Pattern hybride : le KG est source de vérité, mais quand on a un
   doc Revit en main, un read-back préalable est gratuit et évite de
   renvoyer une valeur potentiellement périmée. Le champ `stale: bool`
   dans la réponse permet au LLM de décider s'il doit suggérer un
   `rooms_recompute_boundaries`.

6. **Pré-check de collision de nom pour les Levels.** `levels_create`
   et `levels_set_name` vérifient côté KG qu'aucun autre Level vivant
   n'utilise déjà le nom demandé. Sans ce pré-check, Revit lèverait une
   `InvalidOperationException` brute — message moins lisible pour le
   LLM. Le check exclut les Level soft-deleted (légal de réutiliser un
   nom libéré).

7. **Drift detection sur strings (name) en open-coded.** `detect_drift`
   couvre scalaires/vecteurs/None mais pas les chaînes (concept de
   tolérance n'a pas de sens). Les setters `rooms_set_name` et
   `levels_set_name` font une comparaison directe `actual != requested`
   et formattent leur propre `drift_note`. Pas de helper partagé pour
   ce cas — 4 lignes par tool, factoriser serait prématuré.

### Phase 1 — Plomberie Room côté `kg_sync.py` + `revit_primitives.py`

`revit_primitives.py` :
- **`rooms(doc)`** : collector via `collect_by_category(doc, OST_Rooms)`.
  Pas de tri placed/unplaced — le converter lit `Location` défensivement.

`kg_sync.py` :
- **`_room_to_attrs(room, *, level_ref)`** : extrait `name` (BIP
  `ROOM_NAME`, fallback `"Room"` si vide pour respecter le required du
  schéma), `area` (BIP `ROOM_AREA`, converti via `internal_to_sqm`),
  `boundary_walls=[]` (deferred). `level_ref` fourni par le caller, pas
  re-lu depuis `room.LevelId` (mêmes raisons que les openings — host /
  level ref dérivés côté KG sont autoritatifs).
- **Branche `full_rescan` rooms** : itère `rp.rooms(doc)`, résout
  `level_ref` via `llm_id_of(kg, r.LevelId)`, skip si non bindé. Pose
  l'arête `Room → Level` via `at_level`. Compteur `skipped["rooms"]`
  ajouté.
- **`_REFRESH_FIELDS["Room"] = ("name", "area")`** — les deux attrs
  volatiles. `level_ref` exclu (n'est pas modifié par un read-back
  géométrique ; un futur `rooms_move_to_level` posera un nouveau
  level_ref explicitement). `boundary_walls` exclu (pas calculé).
- **Dispatch `refresh_node_from_revit`** : branche `node_type == "Room"`
  qui passe `level_ref=node.get("level_ref", "")` au converter.
- **Summary `full_rescan`** : `rooms: kg.count_by_type("Room")` ajouté.

`tests/test_kg_sync.py` : stub `_install_rescan_stub` étendu avec
`stub.rooms = lambda doc: []` (sinon `AttributeError` au call
`rp.rooms(doc)`).

### Phase 2 — `tools/rooms.py` (5 tools)

Pattern doc-aware standard (`walls.py` / `openings.py`). 5 tools tier-1 :
- **`rooms_create(level_ref, point, name?)`** : `doc.Create.NewRoom(level, UV)`
  + `doc.Regenerate()` post-placement (sinon `ROOM_AREA=0` même
  enveloppe fermée). Pose le nom si fourni avant Regenerate. Read-back
  via `refresh_node_from_revit` pour mirror l'aire effective. Réponse
  inclut `note: str | None` qui prévient le LLM si `area=0`.
- **`rooms_set_name(llm_id, name)`** : `param.Set(name)` sur
  `ROOM_NAME`. Refus chaîne vide / whitespace-only. Drift detection
  open-coded.
- **`rooms_recompute_boundaries(llm_id?)`** : `doc.Regenerate()` puis
  read-back par room (ciblé ou tous). Réponse compacte par room.
- **`rooms_get_area(llm_id)`** : KG-read + optionnel read-back si doc
  présent. `stale: bool` dans la réponse.
- **`rooms_delete(llm_id)`** : symétrique à `walls_delete`. Soft KG +
  hard Revit.

`_record_in_kg` interne pose `Room` avec `boundary_walls=[]`. Une seule
arête à la création : `at_level`. Pas de read-back drift à la création
(création = pas de "requested" à comparer, cohérent avec décision 1 de
la session 5).

### Phase 3 — `tools/levels.py` (3 tools)

3 tools tier-1 :
- **`levels_create(name, elevation_m)`** : `Level.Create(doc, elev_ft)`
  (static factory moderne, voir REVIT_API_NOTES). Revit auto-nomme
  ("Level 3" / "Niveau 3" selon locale) — renomme via
  `level.Name = new_name` après la création. Pré-check collision côté
  KG avant ouverture de la Tx Revit (rapide, lisible).
- **`levels_set_elevation(llm_id, elevation_m)`** : `level.Elevation =
  meters_to_internal(elev)` (propriété writable directement). Read-back
  + `detect_drift` numérique sur l'élévation.
- **`levels_set_name(llm_id, name)`** : `level.Name = new_name` (idem).
  Pré-check collision excluant le node courant. Drift detection
  open-coded sur la chaîne.

`_REFRESH_FIELDS["Level"]` était déjà déclaré session 5 — rien à
toucher. Pas de `_record_in_kg` pour delete : `levels_delete` n'existe
pas (cf. décision 2).

### Phase 4 — `catalog_list_rooms` + tests

`tools/catalog.py` : ajout de `catalog_list_rooms` (symétrique
`_doors` / `_windows`). Retourne `{llm_id, name, level_ref, area_m2}`
par room vivante.

`tests/test_tools.py` : 17 nouveaux tests KG-only (pas de stub Revit
nécessaire — la branche `doc is None` est exercée) :
- 4 `rooms_create` (création OK, refus level inconnu / non-Level,
  nom par défaut "Room").
- 2 `rooms_set_name` (KG-only no drift, refus empty).
- 1 `rooms_get_area` (stale=True quand pas de doc).
- 2 `rooms_recompute_boundaries` (tous / ciblé llm_id).
- 1 `rooms_delete` (soft delete).
- 1 `catalog_list_rooms` (filtre les soft-deleted).
- 3 `levels_create` (création, refus doublon, refus empty).
- 1 `levels_set_elevation` (KG-only no drift).
- 2 `levels_set_name` (KG-only no drift, refus doublon).

Le registry expected set du test
`test_canonical_registry_has_expected_tier1_tools` est étendu de 8
entrées (`rooms_*` × 5 + `levels_*` × 3 + `catalog_list_rooms`) — soit
9 nouvelles entrées exactement. Le test continue de passer (`issubset`).

### Validation

- `pytest -q` (suite complète, 245 tests) : **245 verts en 7.65s**.
- Régression initiale : 4 tests `test_full_rescan_*` cassés sur
  `AttributeError: module 'lib.revit_primitives' has no attribute 'rooms'`.
  **Cause racine** : le stub `_install_rescan_stub` dans
  `test_kg_sync.py` n'avait pas été mis à jour avec la nouvelle
  itération sur `rp.rooms(doc)` dans `full_rescan`. **Fix** : ajout
  d'une ligne `stub.rooms = lambda doc: []` dans le stub. La leçon —
  toute extension de `full_rescan` doit accompagner son stub côté
  tests. Note pour la suite : un futur Sem.4-5 tool (DWG ingest) qui
  ajouterait un nouveau collector devra mettre à jour le stub aussi.

Compteur de tests : 228 (session 5) → 245 (session courante), soit +17.

### Validation runtime — pas tenté ce tour

Décidé de **ne pas** lancer un test live Revit ce tour-ci : les tools
sont KG-only-testable, la plomberie Revit est *isolée* dans le converter
+ `Document.Create.NewRoom` (déjà éprouvé sur openings via le même
pattern doc-aware), et la dette des sessions précédentes (setters
multi-objets, voir « Reste à faire ») a plus de valeur runtime que ce
chemin déterministe. Test live à prévoir à la prochaine session Revit,
de préférence couplé à un scénario UC2/UC3 réaliste (créer un
appartement-type avec 4 rooms nommées et récupérer leurs aires).

### État final & reste à faire

**Acquis session 2026-05-12** :
- `rooms.py` (5 tools : create, set_name, recompute_boundaries,
  get_area, delete) ✓
- `levels.py` (3 tools : create, set_elevation, set_name) ✓
- `catalog_list_rooms` ✓
- Plomberie kg_sync : `_room_to_attrs`, branche `full_rescan`,
  `_REFRESH_FIELDS["Room"]`, dispatch `refresh_node_from_revit` ✓
- Collector `rp.rooms(doc)` ✓
- Stub `test_kg_sync.py` étendu ✓
- 17 tests, baseline **245 verts** ✓
- §9 V0 Sem.2-3 **bouclé** — toute la géométrie de base couverte côté
  écriture.

**Dettes / TODO ouverts (héritage + nouveaux)** :

1. **Setters multi-objets** (dette session 5, toujours ouverte) —
   gain mesuré ~44 tool_use blocks → ~2 pour 20 fenêtres. Avec rooms
   maintenant en place, on peut chiffrer un scénario type :
   « renomme toutes les rooms du N01 en concaténant le numéro » →
   N tool_use blocks. Si N >= 5, ROI évident. À trancher avec UC8
   compliance qui réclamera des bulk reads + bulk writes sur
   `Room.use_subcategory`. Avec une vue maintenant complète des
   mutants (15 sur 16, seul `walls_delete`/`openings_delete`/`rooms_delete`
   restent solo), on a la matière pour décider entre :
   - setters `*_many` ciblés par paire (mécaniste, lisible),
   - `tools/bulk.py` générique (`apply_to_filter`, Sem.4-5 du plan).
2. **`boundary_walls` non calculé** (nouvelle dette V0 → V1) — à
   activer lors de l'arrivée du modèle compliance (§4.5 du DESIGN).
   Chemin : `room.GetBoundarySegments(SpatialElementBoundaryOptions())`,
   itérer les loops, matcher chaque `seg.ElementId` contre la KG via
   `find_by_revit_id`. Skipper les room separation lines (à terme
   un node `RoomSeparator` ou simplement filtré). Quand activé,
   ajouter `boundary_walls` à `_REFRESH_FIELDS["Room"]` (pour le
   recompute après modification de mur).
3. **`levels_delete` reporté** — voir décision 2.
4. **Validation runtime Revit pour rooms / levels** — non tenté ce
   tour. Scénario test type pour la prochaine session Revit :
   « crée un niveau N02 à 6 m, place une room au centre du RDC et
   nomme-la 'Salon', recompute les aires, donne-moi le total m² du
   N00 ». Devrait exercer : `levels_create` + `rooms_create` +
   `rooms_set_name` + `rooms_recompute_boundaries` + `catalog_list_rooms` +
   un éventuel `aggregations_*` (existant ? à vérifier — sinon pas
   un blocker).

**Suite immédiate (§9 V0 Sem.4-5)** :
- `dwg_reader.py` + `dwg_classifier.py` (ezdxf) → UC1 (import DWG
  paramétré comme calque de murs / fenêtres).
- `tools/bulk.py` (`apply_to_filter`, `change_param_bulk`) → UC7
  (modifications en masse). Couvre potentiellement la dette 1.

**Couverture de risque** : aucune nouvelle exposition. La discipline
read-back est respectée par construction sur tous les nouveaux
mutants ; les pré-checks de collision sur les Levels rendent l'erreur
lisible avant de toucher à Revit.

---

## 2026-05-11 (session 5) — Discipline read-back KG↔Revit systématique sur tout objet

### Contexte & objectif

La validation runtime des openings (session 4) a révélé une **drift
KG↔Revit** sur `window_016` : après un appel multi-tour pour ajuster
toutes les fenêtres (sill=0.80 m, head=2.20 m), le KG indiquait bien
les valeurs demandées, mais Revit affichait `sill=1.45 m / head=2.20 m`
sur cette instance spécifique. Diagnostic : la famille de
`window_016` a `opening_height=0.75 m` (paramètre de TYPE), donc Revit
impose `head − sill = 0.75`. Le LLM a successivement appelé
`set_sill_height(0.80)` puis `set_head_height(2.20)` — la deuxième
écriture (head) a sticked et Revit a recomputé `sill` à `2.20 − 0.75
= 1.45`. **Côté KG, on avait écrit ce qu'on demandait, pas ce que
Revit a réellement committé.**

Le pattern bug est plus large que les openings : tout tool qui ferme
sa transaction sans relire l'état Revit peut laisser le KG diverger.
Demande utilisateur : « ce mécanisme de drift modèle/KG devrait être
appliqué **systématiquement** pour éviter toute différence » +
« systématiquement à **tout objet** je veux dire ». Cette session
établit cette discipline comme invariant d'architecture.

### Décisions

1. **Discipline read-back universelle** : après chaque mutation Revit
   (`param.Set`, `Wall.Create`, `MoveElement`, `NewFamilyInstance`,
   etc.), le tool relit l'élément depuis Revit via un helper central
   et mirror les attrs dans le KG. Le KG **ne fait jamais confiance
   à la valeur demandée** — il mirror la valeur committée. Cas où
   Revit recompute / refuse silencieusement :
   - Familles avec dimensions de type rigides (sill ↔ head couplés
     par `opening_height`).
   - Walls avec Top Constraint qui figent la hauteur indépendamment
     de `WALL_USER_HEIGHT_PARAM`.
   - Placement snap-to-grid sur instances hostées.
   - Locked alignments sur les MoveElement.

2. **Helper central `kg_sync.refresh_node_from_revit(kg, doc, llm_id)`**
   plutôt que pattern dupliqué dans chaque tool. Dispatch par node
   type vers le bon `_*_to_attrs` (Wall / Column / Door / Window /
   ModelLine / DetailLine / Level / WallType / ColumnType / FamilyType
   — 10 types couverts dès aujourd'hui). Une seule source de vérité
   pour la convention « attrs volatiles vs refs immuables ».

3. **Whitelist `_REFRESH_FIELDS`** au lieu d'overwrite global. Pour
   chaque node type, on déclare les attrs *volatiles* à mirror
   (`p1, p2, length, height` pour Wall, `position, sill_height,
   head_height` pour Door/Window, etc.). Les refs (`level_ref /
   type_ref / host_wall_ref / wall_type_ref`) sont *exclues* — elles
   sont fixées à la création et ne changent jamais sous une
   mutation géométrique. Économise les validations de schéma et
   évite d'écraser une référence si jamais une mutation Revit
   touchait la `Host` (rare mais possible).

4. **Helper de comparaison `detect_drift(requested, committed,
   field)`** retournant `(drift: bool, note: Optional[str])`. Gère :
   - Scalaires (différence absolue).
   - Vecteurs `[x, y]` / `[x, y, z]` (élémentwise, max écart).
   - `None` des deux côtés (silent — pas de signal sur ce qu'on ne
     sait pas comparer).
   - Shape mismatch entre requested et committed (flagué comme drift
     suspect).
   - Tolérance `5e-4 m` (½ mm) — absorbe le round-trip pieds↔mètres
     sans flagger des faux drifts.

5. **Symétrie sill ↔ head dans les setters d'openings.**
   `openings_set_sill_height` ne se contente pas de relire `sill_param`
   après `Set` — il relit **aussi** `head_param`, car la contrainte
   familiale `head = sill + opening_height_of_type` peut décaler les
   deux. Le KG mirror les deux valeurs ; la `drift_note` pointe vers
   `openings_set_type` / `openings_create_type_variant` comme
   contournement.

6. **`transforms._refresh_kg_geometry` délégué au helper central**
   pour DRY. L'ancienne version privée couvrait 3 types (Wall, Column,
   Line) — elle aurait silencieusement perdu Door/Window après leur
   ajout en session 4. Délégation à `kg_sync.refresh_node_from_revit`
   couvre les 10 types d'un coup.

7. **Réponse uniforme entre KG-only et Revit path.** Les tools
   refactorés exposent toujours `requested_<field>`, `drift`,
   `drift_note` — en KG-only `drift=False` et `requested=committed`
   par construction. Le LLM voit le même shape, peut écrire un seul
   code path de traitement, et la discipline reste lisible dans les
   tests hors-Revit.

8. **`drift` reporté dans le payload, pas dans le system prompt.**
   Le LLM lit le tool_result et adapte sa réponse à l'utilisateur
   (alerte explicite si `drift: true`). Pas de règle système à
   maintenir — la sémantique vit dans la donnée retournée. Tom Note
   intègre déjà le pointeur vers le contournement (`openings_set_type`
   pour les openings, mention Top Constraint pour `walls_set_height`).

### Phase 1 — Fix immédiat `openings_set_sill_height` / `_set_head_height`

Refactor pour relire `INSTANCE_SILL_HEIGHT_PARAM` **et**
`INSTANCE_HEAD_HEIGHT_PARAM` après chaque `param.Set` (peu importe
lequel des deux a été demandé) — la contrainte familiale couple les
deux. KG mirror les deux valeurs commitées. Helper privé
`_read_sill_head_m(element)` factorise la lecture. Helper privé
`_drift_note(field, requested, actual_sill, actual_head)` produit le
texte explicatif pointant vers `openings_set_type` /
`_create_type_variant`. Le tolérancement `_DRIFT_EPSILON_M = 5e-4`
absorbe le bruit numérique.

Format de la réponse étendu :
- `sill_height_m` / `head_height_m` = valeurs committées (relues).
- `requested_<field>_m` = ce que l'utilisateur a demandé.
- `drift: bool` + `drift_note: str | None`.
- `revit_modified: bool`.

### Phase 2 — Audit + helpers centraux

`lib/kg_sync.py` :
- **`refresh_node_from_revit(kg, doc, llm_id) -> Optional[Dict]`** :
  lit `_revit_id`, fait `doc.GetElement(ElementId(raw))`, dispatch
  sur le `_*_to_attrs` du type, `modify_node` les fields whitelistés.
  Returns `None` si pas bindé / element absent.
- **`_REFRESH_FIELDS: Dict[str, Tuple[str, ...]]`** : whitelist
  par type. 10 entrées (Wall, Column, Door, Window, ModelLine,
  DetailLine, Level, WallType, ColumnType, FamilyType). Une seule
  source à mettre à jour si on ajoute un type.
- **`detect_drift(requested, committed, field='value') -> (bool,
  Optional[str])`** : voir Décision 4.
- **`_DRIFT_EPSILON = 5e-4`** constante module.

Audit des tools : 9 chemins de mutation sans read-back identifiés
(walls × 4, columns × 4, plus walls_move qui calcule sa nouvelle
position mais ne relit pas).

### Phase 3 — Refactor walls

`lib/tools/walls.py` :

- **`walls_create`** : appel `refresh_node_from_revit(kg, doc, llm_id)`
  après `Wall.Create` + `set_revit_id` + `stamp_llm_id`. Réponse
  étendue avec `p1_m / p2_m / length_m / height_m` (actuels).
- **`walls_create_many`** : read-back per-wall après la boucle de
  création (boucle séparée pour ne pas re-Get chaque élément avant
  son commit). Idem pattern bulk activation des FamilySymbols.
- **`walls_move`** : remplace `kg.modify_node({p1, p2})` par
  `refresh_node_from_revit`. Compare au `new_p1 / new_p2` calculé →
  emit `drift` + `drift_note`. KG-only path : réponse compatible
  (drift=False, requested=committed).
- **`walls_set_height`** : remplace `kg.modify_node({height})` par
  `refresh_node_from_revit`. Compare la `height` committée à
  `height_m` demandé → `drift_note` enrichi d'un pointeur Top
  Constraint quand divergence (cas typique : un mur ancré à un
  niveau supérieur ignore la hauteur libre).

### Phase 4 — Refactor columns

`lib/tools/columns.py` :

- **`columns_create`** : read-back après `NewFamilyInstance` +
  `FAMILY_TOP_LEVEL_OFFSET_PARAM.Set`. Réponse expose `position` et
  `height_m` actuels — snap-to-grid sur les grilles Revit
  potentiellement visible ici.
- **`columns_create_many`** : read-back per-column en fin de boucle
  (idem walls). `columns_create_grid` / `_grid_irregular` héritent
  automatiquement via leur délégation à `columns_create_many`.

### Phase 5 — Refactor transforms

`lib/tools/transforms.py` :

`_refresh_kg_geometry` réduit à 4 lignes — délègue à
`kg_sync.refresh_node_from_revit` pour chaque `llm_id`. Couvre
maintenant Door/Window automatiquement (gain par rapport à l'ancienne
version qui aurait perdu ces types post-session 4). Tous les 7
transforms `elements_*` héritent.

### Phase 6 — Tests (+14)

`tests/test_kg_sync.py` (+6) :
- `refresh_node_from_revit` retourne None sur node sans `_revit_id`
  binding (CLI path).
- Retourne None sur llm_id inconnu (pas de KeyError).
- `detect_drift` no-drift dans la tolérance (5e-4 m).
- `detect_drift` scalaire reporte la différence.
- `detect_drift` vecteur compare elementwise.
- `detect_drift` tolère `None` silencieusement.
- `detect_drift` vector length mismatch → drift suspect.

`tests/test_tools.py` (+5 dont 3 drift sill/head session-spécifique +
2 walls drift fields) :
- `openings_set_sill_height` KG-only expose `requested_sill_height_m`,
  `drift=False`, `drift_note=None`, `head_height_m` reporté.
- Symétrique pour `openings_set_head_height`.
- `_drift_note` helper test direct (avec / sans drift / avec None
  actual).
- `walls_set_height` réponse contient `requested_height_m`, `drift`,
  `drift_note` même en KG-only.
- `walls_move` réponse contient `requested_p1_m`, `requested_p2_m`,
  `drift`, `drift_note` même en KG-only.

### Validation

- `pytest -q` : **228 tests verts en ~7s** (214 → +14). Aucune
  régression. Le compteur a augmenté par étapes :
  - 214 → 219 : fix immédiat openings_set_sill_height / _set_head_height
    + tests de structure de réponse.
  - 219 → 225 : helpers `refresh_node_from_revit` + `detect_drift`
    + tests unitaires des helpers.
  - 225 → 228 : refactor walls + tests structure de réponse drift
    sur `walls_set_height` / `walls_move` en KG-only.

### Validation runtime — confirmée le soir même

Re-scénario sur le projet test, **20 fenêtres mixées sur 4 types
familiaux**, prompt « passe toutes les fenêtres à sill=0.80,
head=2.20 ». Résultat :

- **`drift=False` sur les 20** — l'invariant tient. Le LLM n'a pas
  enchaîné les deux setters à l'aveugle (ce qui aurait fait drift
  sur les 4 types dont `opening_height ≠ 1.40 m`) ; il a choisi
  d'abord de **changer la hauteur d'ouverture côté type**.
- Séquence exécutée : 4 × `openings_create_type_variant` (une variante
  par famille avec `opening_height=1.40 m`) → 20 × `openings_set_type`
  (swap chaque fenêtre vers sa nouvelle variante) → 20 ×
  `openings_set_sill_height(0.80)` (la contrainte type devient
  cohérente, head suit à 2.20 sans recompute parasite).
- Le LLM **verbalise la stratégie** dans sa réponse utilisateur :
  « La clé était d'abord changer le type avec la bonne hauteur
  d'ouverture (1,40 m), puis de setter l'allège ». La sémantique
  encodée dans `openings_set_type` / `_create_type_variant` (et les
  `drift_note` des sessions précédentes) suffit à l'orienter — pas
  besoin de règle système supplémentaire.
- Coût : 4 round-trips API (87 K input / 3.8 K output, cache_read
  31 K / cache_write 35 K). ~44 tool calls. Le cache write élevé
  reflète les `tool_result` qui empilent à chaque round-trip (le
  préfixe statique se cache bien, c'est l'historique de tour qui
  grossit).

### Couverture de la discipline post-session 5

Tools mutants qui appellent maintenant `refresh_node_from_revit` (ou
équivalent) après leur mutation :

| Tool | Helper appelé | Drift fields exposés |
|------|---------------|----------------------|
| `walls_create` | ✓ direct | (création — pas de drift par construction) |
| `walls_create_many` | ✓ boucle | idem |
| `walls_create_polyline` / `_from_lines` | ✓ via `_create_many` | idem |
| `walls_move` | ✓ direct | `drift` + `drift_note` + `requested_p1_m` / `_p2_m` |
| `walls_set_height` | ✓ direct | `drift` + `drift_note` (Top Constraint) + `requested_height_m` |
| `walls_delete` | n/a (suppression) | — |
| `columns_create` | ✓ direct | (création) |
| `columns_create_many` / `_grid` / `_grid_irregular` | ✓ boucle | idem |
| `openings_create_*` | déjà ✓ (session 4) | (création) |
| `openings_set_sill_height` | ✓ direct (sill + head re-lus) | `drift` + `drift_note` |
| `openings_set_head_height` | ✓ direct (sill + head re-lus) | `drift` + `drift_note` |
| `openings_set_type` | déjà ✓ (session 4) | (sill/head re-lus post-swap) |
| `openings_create_type_variant` | déjà ✓ (session 4) | (dimensions re-lues post-Set) |
| `openings_delete` | n/a (suppression) | — |
| `elements_translate` / `_rotate` | ✓ via `_refresh_kg_geometry` (→ helper central) | (transforms in-place) |
| `elements_mirror` / `_copy` / `_array_*` | ✓ via `ingest_revit_element` (re-lit dès la copie) | (création de copies) |

**14 tools mutants** sur 16 ont le read-back ; les 2 restants
(`walls_delete`, `openings_delete`) n'en ont pas besoin (suppression
unidirectionnelle).

### État final & reste à faire

**Acquis session 5** :
- Helper central `refresh_node_from_revit` (10 types couverts) ✓
- Helper `detect_drift` (scalaire / vecteur / None / shape mismatch) ✓
- `openings_set_sill_height` / `_set_head_height` relisent sill ET
  head avec drift_note pointant vers `openings_set_type` ✓
- Refactor walls : `_create`, `_create_many`, `_move`,
  `_set_height` avec read-back systématique ✓
- Refactor columns : `_create`, `_create_many` (grid / irregular
  héritent) avec read-back ✓
- `transforms._refresh_kg_geometry` délégué au helper central →
  couvre Door/Window automatiquement ✓
- Réponses uniformes drift / drift_note / requested_* entre KG-only
  et Revit path ✓
- 14 nouveaux tests, baseline **228 verts** ✓

**Dettes / TODO créés** :
1. **`detect_drift` n'est appelé que sur les setters de valeur**
   (`walls_set_height`, `walls_move`, `openings_set_*_height`). Sur
   les créations (`*_create_*`), le read-back mirror sans flagger
   de drift — on n'a pas de "requested" à comparer dans le cas
   création. Si on voulait flagger un snap-to-grid à la création
   (ex : « j'ai demandé p1=[0.524, 3.955] mais Revit a snappé à
   [0.5, 4.0] »), il faudrait passer les valeurs demandées au
   helper et étendre. Pas urgent — détecter au runtime si le cas
   se présente.
2. **Pas de pré-check de feasibility** côté openings : si
   `family_height` est connu (via `FamilyType.dimensions`), on
   pourrait anticiper le drift avant le Set et alerter le LLM en
   amont (« sill=0.80 + family_height=0.75 = head=1.55, donc le
   2.20 demandé ne tiendra pas »). Plus ergonomique, mais
   demande de propager `dimensions.height_m` partout. Repoussé.
3. **Helper read-back par catégorie d'attrs** : on relit TOUS les
   fields whitelistés à chaque mutation, même si seul un changeait.
   Coût acceptable (un Element fetch + un appel converter) mais
   surveillable sur des bulk size > 100. Optimisation tardive si
   le profil le réclame.
4. **Setters multi-objets manquants** (révélé par la validation
   runtime — voir ci-dessus, ~44 tool calls pour 20 fenêtres).
   Symétrique au pattern `*_create_many` livré session 4, on
   gagnerait à exposer :
   - `openings_set_type_many(items=[{llm_id, new_family_type_ref}, …])`
   - `openings_set_sill_height_many(items=[{llm_id, sill_height_m},
     …])` (et `_head_height_many` par symétrie)
   - Variante filter-based : `openings_set_sill_height_bulk(filter={…},
     sill_height_m=…)` qui résout le filtre côté KG et applique en
     un appel. Le filtre serait `{level_ref, type_ref,
     host_wall_ref, llm_id_prefix, …}` avec match all-of-fields.
   - Idem pour `walls_set_height_many` et `walls_move_many`
     (configurations courantes : aligner toutes les fenêtres d'un
     niveau, lever la hauteur d'une série de murs).
   Estimation gain : ~40 tool_use blocks → ~2 tool_use blocks sur le
   scénario du soir. Cache hit rate meilleur, latence /20. À chiffrer
   après l'arrivée de `rooms` qui pourrait aussi en bénéficier
   (`rooms_set_use_subcategory_many` pour le scope hiérarchique
   compliance §4.5).

**Suite immédiate (§9 DESIGN, Semaines 2-3 V0 restant)** :
- `rooms.py` : create, recompute_boundaries, set_name, get_area +
  convertisseur Room. La discipline read-back s'appliquera dès
  l'écriture des tools (helper central déjà prêt à accueillir Room
  dans `_REFRESH_FIELDS`).
- `levels.py` (écriture) : create, modify_elevation, set_active.

**Suite Sem.4-5 V0** :
- `dwg_reader.py` + `dwg_classifier.py` (ezdxf) → UC1.
- `tools/bulk.py` (`apply_to_filter`, `change_param_bulk`) → UC7.

---

## 2026-05-11 (session 4) — Openings (portes/fenêtres) + préprocesseur déterministe + découplage sill/head

### Contexte & objectif

Démarrage de Semaines 2-3 V0 (§9 DESIGN) sur le morceau `openings.py`,
qui débloque la majorité des prompts naturels architecturaux (« ajoute
une porte », « passe l'allège des fenêtres à 1 m », etc.). Trois pans
abordés en une session :

1. **`openings.py` bout-en-bout** : schéma KG, collectors, convertisseurs,
   full_rescan étendu, ~6 tools, catalogues, `ingest_revit_element` à
   jour pour que les 7 transforms couvrent automatiquement Door/Window.
2. **Bug détecté en runtime** : « passe toutes les fenêtres à 1 m
   d'allège » a traité 2/3 fenêtres seulement. Le LLM s'est fié à sa
   mémoire conversationnelle au lieu d'appeler `catalog_list_windows`.
   Une règle dans le system prompt est *advisory* — le modèle peut
   l'ignorer sous pression. Fix : pré-processeur déterministe
   `lib/preprocess.py` qui détecte les quantificateurs universels
   dans le prompt utilisateur AVANT l'appel API et injecte le résultat
   du `catalog_list_*` correspondant.
3. **Découplage sill / head** : modifier `INSTANCE_SILL_HEIGHT_PARAM`
   décale automatiquement `INSTANCE_HEAD_HEIGHT_PARAM` parce que la
   plupart des familles Revit ont la hauteur d'ouverture en *paramètre
   de type*. Solution BIM-orthodoxe : `openings_set_type` (switch
   FamilySymbol) + `openings_create_type_variant` (duplique le type
   avec une autre hauteur d'ouverture).

### Décisions

1. **FamilyType générique avec attribut `category` discriminator**
   plutôt que `DoorType` / `WindowType` séparés. Évite la prolifération
   de node types pour les hosted families (futures : Furniture,
   Equipment, etc.). Le catalogue filtre par `category` ; la symétrie
   avec `ColumnType` (qui garde son node type à cause du discriminator
   `kind: architectural | structural`) est gardée à l'esprit — un node
   type spécifique se justifie quand un attribut spécifique l'impose.

2. **Position openings = `[x, y]` plan du niveau** (mètres). z dérivé
   du host (élévation du niveau hôte + sill_height). Aligné avec
   `Column.position` et `Wall.p1/p2`. La 3ᵉ coordonnée serait bruit
   redondant (l'edge `at_level` la porte déjà).

3. **Edge `hosts` orienté Wall → opening** (le mur héberge la porte) ;
   `is_type` opening → FamilyType ; `at_level` dérivé du host wall via
   `host_attrs.level_ref`, pas de `door.LevelId` (sur certaines
   familles hosted la propriété Level peut différer du host — on
   considère le host comme autorité pour l'étage).

4. **Quantificateurs universels → autoscan déterministe côté
   pushbutton, pas instruction system prompt.** L'incident runtime a
   confirmé que le LLM peut ignorer une règle texte. Le pré-processeur
   scanne le prompt avec une regex (`toutes/tous/chaque/l'ensemble
   des/all/every/each` + noun de collection FR+EN, tolérance
   accent-less), dispatche les `catalog_list_*` localement, et
   préfixe le résultat dans un bloc `<auto_scan_kg>` du message
   utilisateur. Le LLM voit l'énumération exhaustive avant même de
   choisir un tool — plus de marge pour oublier. Coût tokens payé
   uniquement quand un quantificateur apparaît effectivement.

5. **Soft lock résiduel sur les quantificateurs non couverts.** Si
   l'utilisateur dit « pour chacun des éléments du R+1 » (collection
   pas dans la mappe regex), le pré-processeur ne déclenche pas → la
   règle texte du system prompt reste comme filet de sécurité. Le
   modèle est instruit que **quand il voit `<auto_scan_kg>`, il NE
   doit PAS rappeler `catalog_list_*`** pour les collections déjà
   listées (économie tokens) ; sinon, il appelle lui-même.

6. **Découplage sill ↔ head via assignment de type, pas via override
   instance.** Choix BIM-orthodoxe (option A) : l'utilisateur passe à
   un FamilyType qui a déjà la bonne `dimensions.height_m`. Quand le
   catalogue ne contient pas de variant adapté (option B),
   `openings_create_type_variant` duplique un FamilySymbol et règle
   sa hauteur d'ouverture côté type — pas de touch instance, ce qui
   évite le piège « le param Height est read-only / type-level sur la
   plupart des familles standard ».

7. **Cascade BuiltInParameter → LookupParameter pour les dimensions
   d'ouverture.** Les noms varient selon la famille / vendor / langue :
   `WINDOW_HEIGHT` / `DOOR_HEIGHT` / `FAMILY_HEIGHT_PARAM` /
   `GENERIC_HEIGHT` essayés d'abord, puis `LookupParameter("Height" |
   "Hauteur" | "Hauteur d'ouverture")` en fallback (et idem largeur).
   Toutes les opérations sont silent-fail : `None` au read, `False` au
   set. Le tool `openings_create_type_variant` remonte un message
   actionnable seulement si **toute** la cascade échoue.

8. **`FamilyType.dimensions` populé opportunistement au rescan.**
   Quand la cascade `opening_read_height_m` retourne une valeur, on
   stamp `dimensions: {height_m, width_m}` sur le node — sinon on
   omet l'attribut. Le LLM peut alors filtrer dans
   `catalog_list_door_types` / `_window_types` sur les dimensions
   exposées sans avoir à interroger l'API Revit.

9. **`ProjectKG.remove_edge(src, dst, edge_type)` méthode publique**
   pour permettre `openings_set_type` de re-router l'edge `is_type`
   atomiquement. Évite le pattern `_g.remove_edge` underscore-private
   dans le code applicatif. Idempotente (retourne True/False).

### Phase 1 — Schéma KG : `FamilyType.category` required

`lib/project_kg.py` : `FamilyType` étendu avec `category` en required
("Doors" | "Windows" | …). Door / Window déjà déclarés (héritage du
slice initial). Pas de nouveau node type — la discrimination est
attributaire.

### Phase 2 — Collectors + convertisseurs + full_rescan étendu

`lib/revit_primitives.py` :
- `doors(doc)` / `windows(doc)` (instances).
- `door_types(doc)` / `window_types(doc)` (FamilySymbols).

`lib/kg_sync.py` :
- `_family_type_to_attrs(symbol, *, category)` — populé dimensions
  opportunistement (cf. Décision 8).
- `_opening_to_attrs(opening, *, type_ref, host_wall_ref)` — position
  2D métres, sill/head via BuiltInParameters, arrondi 6 décimales.
- 4 passes ajoutées à `full_rescan` : DoorTypes, WindowTypes, Doors,
  Windows. Le summary élargi expose des counts séparés pour
  `door_types` / `window_types` (filtrés sur l'attribut `category` du
  node FamilyType).
- `ingest_revit_element` : nouvelle branche `FamilyInstance` qui
  détecte `Category.Id ∈ {OST_Doors, OST_Windows}` et dispatche vers
  `_opening_to_attrs`. Les 7 transforms héritent automatiquement.
- `_RESCANNABLE_CATEGORY_IDS` enrichi de OST_Doors / OST_Windows → la
  sélection active suggère un Refresh KG si l'utilisateur clique un
  opening non bindé.

### Phase 3 — Tools `openings.py` (nouveau, ~720 lignes)

Six tools livrés :

| Tool | Action |
|------|--------|
| `openings_create_door(host_wall_ref, family_type_ref, position, sill_height?)` | Solo, refuse type non-Doors |
| `openings_create_window(host_wall_ref, family_type_ref, position, sill_height?)` | Solo, refuse type non-Windows, défaut sill=0.9m KG-only |
| `openings_create_many(items)` | Bulk doors+windows mixé, validation upfront, batch activation FamilySymbols |
| `openings_set_sill_height(llm_id, sill_height_m)` | `INSTANCE_SILL_HEIGHT_PARAM`, KG + Revit |
| `openings_set_head_height(llm_id, head_height_m)` | `INSTANCE_HEAD_HEIGHT_PARAM`, KG + Revit |
| `openings_delete(llm_id)` | KG soft-delete + Revit `Document.Delete` |

Helper privé `_require_live_opening` (pattern de `_require_live_wall`)
factorise les pré-checks. `stamp_llm_id` appelé après chaque
`set_revit_id`. Pattern doc-aware standard : `doc=None` → mutation KG
seule, `doc != None` → branche Revit avec `rp.transaction(...)`.

### Phase 4 — Catalogues openings + dimensions

`lib/tools/catalog.py` étendu :
- `catalog_list_door_types()` / `_window_types()` — filtrent FamilyType
  par `category`, surface `dimensions` quand présent (key absente si
  la cascade Revit n'a rien trouvé pour ce type).
- `catalog_list_doors()` / `_windows()` — listent les openings vivants
  avec position, sill, head, host_wall_ref.
- Helper privé `_list_family_types_by_category` factorisé.

### Phase 5 — DYNAMIC state block enrichi

`prompt.pushbutton/script.py:_dynamic_state_block` ajoute
`Portes / Fenêtres / Types de porte / Types de fenêtre` au summary
qui apparaît à chaque turn. Les counts FamilyType sont ventilés par
`category` (boucle sur les nodes au lieu d'un simple `count_by_type`)
pour que le LLM voie la composition réelle de son inventaire.

### Phase 6 — Pré-processeur déterministe (`lib/preprocess.py` nouveau)

Surface :
- `detect_exhaustive_collections(prompt) -> [(tool_name, key), …]` :
  regex compilées au load, pattern `(quantificateur)\s*(noun)` avec
  10 entrées (4 type-catalogs prioritaires + 6 instance-catalogs).
  L'ordre dans `_COLLECTION_MAP` privilégie le noun phrase le plus
  spécifique (« types de mur » avant « murs »). De-dup et ordre stable
  par position dans le prompt.
- `autoscan_payload(prompt, kg) -> str` : dispatche les catalogs
  détectés via `llm_protocol.dispatch_tool_use`, formate les résultats
  dans un bloc `<auto_scan_kg>…</auto_scan_kg>` avec un trailing note
  explicite (« exhaustive et à jour ; itère dessus ; ne RAPPELLE
  PAS `catalog_list_*` »). Retourne `""` quand rien ne matche → zéro
  coût sur les prompts ordinaires.

Tolérances regex :
- Sans accent : `fenetres`, `etages` matchent (alternation `[ée]` /
  `[êe]` dans les classes).
- Casse : `re.IGNORECASE`.
- Pluriels : `s?` sur tous les nouns + `toutes?` / `tous`.
- Anglais : `all (the) | every | each` parallèle au français.

### Phase 7 — Wiring autoscan dans `prompt.pushbutton`

Le pushbutton appelle `preprocess.autoscan_payload(user_prompt, kg)`
*avant* `build_user_content`. Si le payload est non vide, il est
préfixé au texte utilisateur — Anthropic le voit comme partie du
message utilisateur. Le pré-processeur est donc transparent côté
LLM-API ; on ne touche pas à l'historique persisté (un autoscan
n'ajoute pas de turn assistant fantôme).

System prompt mis à jour : la règle « quantificateurs universels »
explique le bloc `<auto_scan_kg>` comme énumération faisant autorité
*injectée par le pushbutton* — le LLM doit itérer dessus directement
et **ne pas** rappeler `catalog_list_*`. Le fallback (régex non
matchée → autoscan absent → LLM se débrouille avec un `catalog_list_*`
manuel) reste documenté.

### Phase 8 — Découplage sill / head : helpers + 2 tools

`lib/revit_primitives.py` : 4 helpers cascade pour les dimensions
opening :
- `opening_read_height_m(symbol)` / `opening_read_width_m(symbol)` —
  cascade BIP (`WINDOW_HEIGHT` / `DOOR_HEIGHT` / `FAMILY_HEIGHT_PARAM`
  / `GENERIC_HEIGHT`) puis `LookupParameter` (`Height` / `Hauteur` /
  `Hauteur d'ouverture`, idem largeur). Retournent None si rien.
- `opening_set_height(symbol, value_m)` / `opening_set_width(symbol,
  value_m)` — même cascade en écriture, retournent True/False.
  `IsReadOnly` filtré, silent-fail sur exception. Utilisent
  `getattr(BuiltInParameter, name, None)` pour tolérer une BIP
  absente d'une version Revit donnée — pas d'`ImportError` à
  l'import.

`lib/tools/openings.py` étendu de 2 tools :

- **`openings_set_type(llm_id, new_family_type_ref)`** : switch d'un
  FamilySymbol à un autre sur une porte/fenêtre existante.
  - Validation : nouveau type **même catégorie** (Door ↔ Doors,
    Window ↔ Windows) — refuse cross-category avec message
    actionnable.
  - Revit : `instance.Symbol = new_symbol`, activate si besoin.
  - KG : `remove_edge` ancien `is_type` + `modify_node(type_ref +
    sill_height + head_height)` + `add_edge` nouveau. Re-lit sill /
    head post-swap (le nouveau type peut avoir une hauteur d'ouverture
    différente).

- **`openings_create_type_variant(source_type_ref, new_name,
  opening_height_m, opening_width_m?)`** : duplique un FamilySymbol
  via `symbol.Duplicate(new_name)`, règle la hauteur d'ouverture (et
  largeur si fournie) via les helpers cascade. Refus actionnable si
  toute la cascade échoue (message qui liste les BIPs et les
  LookupParameter tentés). Active le nouveau symbol, bind ElementId,
  stamp llm_id. Ajoute le node FamilyType au KG avec `dimensions`
  re-lues post-Set (mirror de ce que Revit a réellement commit).

Cas d'usage typique enchaîné : « j'ai besoin d'une fenêtre 90cm de
sill et 220cm de head » →
1. `catalog_list_window_types` → cherche un type avec
   `dimensions.height_m == 1.30`.
2. Si absent : `openings_create_type_variant(source_type_ref, "Fenêtre
   1300mm", opening_height_m=1.30)`.
3. `openings_set_type(window_001, new_type_llm_id)`.
4. `openings_set_sill_height(window_001, 0.9)` → head tombe à 2.20
   automatiquement (sill + opening_height).

### Phase 9 — `ProjectKG.remove_edge` publique

Méthode ajoutée à `project_kg.py` : `remove_edge(src, dst, edge_type)
-> bool`. Idempotente — retourne True si l'edge existait, False sinon.
Utilisée par `openings_set_type` pour re-router `is_type` atomiquement
sans accès `_g.remove_edge` underscore-private.

### Validation

- `pytest -q` : **214 tests verts en ~7s** (187 → +27 cette session).
  - 17 nouveaux dans `test_preprocess.py` (détection FR/EN +
    autoscan).
  - 8 dans `test_tools.py` pour les openings de base + 8 supplémentaires
    pour `set_type` / `create_type_variant` + dimensions au catalog.
  - 3 dans `test_project_kg.py` : `FamilyType.category` required,
    `Door/Window` schema, `remove_edge` idempotente.
- **Validation runtime partielle** : openings tools exercés (incident
  des 2/3 fenêtres), pré-processeur et set_type/variant non encore
  validés en Revit (à faire prochaine ouverture).

### État final & reste à faire

**Acquis session 4** :
- Openings complet : 6 tools création/modification + 4 catalogues +
  rescan + ingest_revit_element + DYNAMIC state ✓
- Pré-processeur déterministe `<auto_scan_kg>` : détection FR+EN +
  injection dans le prompt utilisateur ✓
- Découplage sill/head via `openings_set_type` + `_create_type_variant`
  + helpers cascade Height/Width ✓
- FamilyType.dimensions populé au rescan, surface dans les catalogs ✓
- `ProjectKG.remove_edge` méthode publique ✓
- 214 tests verts (187 → +27), aucune régression ✓

**À valider en runtime** :
- Création d'une porte / fenêtre + ajustement sill/head.
- `openings_create_many` avec 10 fenêtres dans un même mur.
- Pré-processeur : prompt « passe toutes les fenêtres à 1 m
  d'allège » → bloc `<auto_scan_kg>` injecté, LLM traite les 3 sans
  oublier.
- `openings_set_type` : swap d'un type vers un autre, sill/head
  re-lus correctement.
- `openings_create_type_variant` : duplication d'un type avec hauteur
  d'ouverture custom, dispo immédiate pour `openings_create_window`.

**Dettes / TODO créés** :
1. **Pré-processeur — collection inconnue** : si l'utilisateur dit
   « pour l'ensemble des escaliers », rien ne se déclenche (collection
   non encore modélisée). Pas de bug, mais le LLM doit tomber sur le
   safety-net texte. À étendre `_COLLECTION_MAP` quand `rooms` /
   `stairs` / `floors` arriveront.
2. **`openings_create_type_variant` — paramètres custom** : seules
   `Height` / `Width` sont exposées (cascade dimensions). Pour des
   familles avec d'autres dimensions paramétrables (épaisseur de
   cadre, hauteur d'imposte, …), l'utilisateur devra ouvrir Revit. À
   évoluer si un cas réel le réclame.
3. **Sill / head dépend de la famille** : `openings_set_sill_height`
   tombe en erreur si la famille a `INSTANCE_SILL_HEIGHT_PARAM`
   read-only (rare mais possible sur certaines familles custom).
   Le message d'erreur pointe la cause — le LLM peut suggérer un
   `set_type` à la place.

**Suite immédiate (§9 DESIGN, Semaines 2-3 V0 restant)** :
- `rooms.py` : create, recompute_boundaries, set_name, get_area +
  convertisseur Room + ingest_revit_element branche Room. Préparation
  UC8 compliance (Room.use_subcategory pour le scope hiérarchique).
- `levels.py` (écriture) : create, modify_elevation, set_active.
  Lecture déjà couverte au rescan.

**Suite Sem.4-5 V0** :
- `dwg_reader.py` + `dwg_classifier.py` (ezdxf) → UC1 (DWG → modèle).
- `tools/bulk.py` (`apply_to_filter`, `change_param_bulk`) → UC7.

---

## 2026-05-11 (session 3) — Stabilisation des `llm_id` au rescan + shared param UX

### Contexte & objectif

La validation runtime de la session 2 (~20:34) a confirmé que les 7
phases livrées par le commit `a97d485` fonctionnent bout-en-bout, mais
a mis en lumière **deux problèmes liés à la même cause racine** :

1. Les `llm_id` ne sont **pas visibles** dans Revit — l'utilisateur
   clique un mur dans le modèle sans pouvoir le nommer (« wall_007 »
   n'apparaît nulle part).
2. Les `llm_id` sont **réassignés silencieusement** au `full_rescan` :
   `_clear_topology()` vide les counters, et le rebuild régénère tous
   les ids depuis 0. Au tour 6 du test runtime, le LLM a appelé
   `walls_delete(wall_006)` → `ValueError: Unknown llm_id: wall_006`
   parce qu'un rescan entre les tours avait renommé `wall_006` en
   `wall_004`. Le LLM s'autocorrige en re-listant, mais supprime
   alors *le mauvais mur*.

Trou design critique pour tout flow conversationnel multi-tour. Cette
session corrige les deux problèmes via la même intervention.

### Décisions

1. **KG = source de vérité du mapping `revit_id ↔ llm_id`. Shared
   parameter Revit = surface UX + fallback de récupération
   uniquement.** Validé explicitement par l'utilisateur. Le param est
   écrit *depuis* le KG après chaque `bind`, jamais lu *par* le KG en
   flow normal. La lecture (`get_llm_id_from_element`) existe mais
   sert uniquement à reconstituer le KG si le JSON disque est perdu
   ou corrompu — pas un chemin de routine.

2. **Stabilité des `llm_id` au rescan via snapshot pré-clear.**
   `full_rescan` capture `{revit_id: llm_id}` depuis `kg._g.nodes`
   *avant* d'appeler `_clear_topology`. Au rebuild, chaque élément
   Revit cherche son revit_id dans le snapshot : si match, on
   réutilise le même `llm_id` (passé explicite à `add_node`) ; sinon,
   le counter alloue un id frais. Les counters sont *préservés* à
   travers `_clear_topology` pour qu'un id frais avance toujours
   au-delà du max déjà alloué — pas de collision possible avec un id
   réutilisé.

3. **Custom shared parameter `claude-in-revit:llm_id` plutôt que
   hijack de `Mark`** (« Identifiant » dans la UI FR). Décision prise
   après proposition utilisateur d'utiliser le champ built-in. Trois
   raisons :
   - **Collision avec données utilisateur** : `Mark` est un champ
     éditable utilisé par les architectes pour les nomenclatures
     (« P-01 », « MUR_PORTEUR_R+1_03 »). L'écraser détruit du
     travail réel.
   - **Pas universel** : `BuiltInParameter.ALL_MODEL_MARK` existe sur
     les instances mais pas sur `WallType`, `ColumnType`, `Level`. On
     aurait quand même besoin d'un shared param custom pour ces
     nœuds → deux systèmes en parallèle.
   - **Cascades dans les nomenclatures** : les schedules built-in
     trient/affichent par Mark, l'hijacker scramble ces vues.
   Le custom param est placé dans `GroupTypeId.IdentityData` →
   apparaît dans Propriétés sous le groupe « Identification », juste
   à côté de Mark. UX native, zéro collision.

4. **Soft lock plutôt que hard lock pour V0.** Revit n'expose pas
   d'API pour rendre un paramètre utilisateur non-éditable dans le
   panneau Propriétés (`Parameter.IsReadOnly` est fixé par la
   définition, pas configurable). Trois options évaluées :
   - **`IUpdater` + `UpdaterRegistry`** : écoute `DocumentChanged`,
     révoque toute édition manuelle en temps réel. Hard lock effectif
     mais ~50 lignes + updater always-on + friction sur bulk edits.
   - **Re-stamp au prochain `Refresh KG`** : l'édition manuelle est
     silencieusement écrasée puisque le KG est source de vérité.
     Soft lock, gratuit avec l'archi en place.
   - **Convention de nommage** : préfixe « (managed) » pour signaler
     « ne pas toucher ». Aucun lock, juste un signal visuel.
   Choisi : le **soft lock** (option 2). L'`IUpdater` reste ouvert si
   un usage réel révèle des éditions manuelles fréquentes.

5. **Binding sur toutes les catégories acceptant un paramètre lié**
   (`Settings.Categories` filtré par `AllowsBoundParameters`) plutôt
   que sur les seules catégories couvertes en V0. Anticipe Doors,
   Windows, Rooms, Floors, Roofs, Beams, etc. — quand on ajoutera un
   convertisseur Element→attrs, le binding sera déjà en place. Coût
   marginal (la `CategorySet` est construite une fois par session).

6. **Action log filtré pendant le rescan.** Avant : N événements
   `create` (un par élément reconstruit) + 1 événement `rescan`.
   Conséquence visible dans le JSON post-validation : action_log
   gonflé de doublons à chaque clic Refresh KG (`level_001`,
   `walltype_001`, `walltype_002` re-créés à chaque turn 0 puis turn
   5 dans le test du 2026-05-11). Fix : `add_node(_emit_log=False)`
   passé pendant le rebuild → seul l'event `rescan` avec son summary
   subsiste. Plus lisible, plus compact, et la timeline conversationnelle
   reste précise (pas de faux « créations » mélangées aux vraies).

7. **Arrondi FP à 6 décimales (1 µm) au boundary SI.** Helper `_r()`
   appliqué dans tous les convertisseurs `_*_to_attrs`. Élimine les
   `0.20000000000000004` et `4.999999999999992` du JSON sans rogner
   sur la précision exploitable (BIM ne raisonne pas sous le µm).

8. **`GroupTypeId.IdentityData` plutôt que `BuiltInParameterGroup.PG_IDENTITY_DATA`.**
   Découvert au premier clic Refresh KG après livraison initiale :
   `ImportError: cannot import name 'BuiltInParameterGroup' from
   'Autodesk.Revit.DB'`. Revit 2024 a déprécié l'enum `BuiltInParameterGroup`
   en faveur du nouveau `GroupTypeId` (`ForgeTypeId`), 2025 a supprimé
   l'ancien du namespace public. Même switch que
   `DisplayUnitType → UnitTypeId` documenté en haut de
   `revit_primitives.py`. Migration straightforward : un import, une
   constante.

### Phase 1 — `ProjectKG` : flags et helpers pour le rescan stable

`lib/project_kg.py` étendu de trois points :

- **`add_node(..., _emit_log: bool = True)`** : flag interne qui supprime
  l'append au `action_log` quand `False`. Utilisé exclusivement par
  `full_rescan` pour éviter N entrées `create` à chaque refresh.
  Documentation explicite : « leave at default for tool code ».
- **`_clear_topology(preserve_counters: bool = False)`** : nouveau
  paramètre. Default `False` préserve la sémantique existante (les
  tests `test_clear_topology_resets_graph_but_preserves_turn_and_history`
  continuent à passer). `True` (passé par `full_rescan`) garde
  `_counters` intact pour que les ids frais avancent au-delà du max
  alloué.
- **`snapshot_revit_id_map() -> Dict[int, str]`** : retourne le
  mapping `{revit_id: llm_id}` pour tous les nœuds bindés (y compris
  soft-deleted, pour qu'un undo → rescan recouvre l'id). Méthode
  publique pour exposer l'intention dans `full_rescan` et faciliter
  les tests.

### Phase 2 — `kg_sync.full_rescan` : snapshot, reuse, log unique

Trois changements structurels :

1. **Snapshot avant clear.** `preserved = kg.snapshot_revit_id_map()`
   capturé en début de fonction. Helper local `_preserved_id(element)`
   qui résout via `_extract_revit_id` (gère le breaking change
   `Value/IntegerValue` 2024) et retourne `Optional[str]`.
2. **Rebuild avec llm_id préservé + `_emit_log=False`** sur les sept
   boucles (Levels, WallTypes, Walls, ModelLines, DetailLines,
   ColumnTypes, Columns). `kg._clear_topology(preserve_counters=True)`.
   Au lieu de générer un id frais à chaque `add_node`, on lui passe
   `llm_id=_preserved_id(element)` — `None` → counter alloue,
   `wall_007` → réutilisation.
3. **Summary élargi** avec `preserved_llm_ids: int` (count des nœuds
   dont le revit_id matchait le snapshot). Permet de diagnostiquer un
   éventuel renumbering en regardant le rapport plutôt qu'en
   comparant deux versions du JSON.

Plumbing supplémentaire pour la surface UX (cf. Phase 3) :
`ensure_shared_param_binding(doc)` appelé hors `kg.transaction()` (il
ouvre sa propre Revit Tx), soft-fail vers `param_bound=False` si le
binding refuse. Le rebuild ouvre alors `rp.transaction(doc, ...)` *si*
le binding est en place, sinon `contextlib.nullcontext()` (préserve le
chemin pure-KG des tests hors-Revit). `_stamp(element, llm_id)` est
appelé après chaque `bind`, no-op transparent quand le binding manque.

### Phase 3 — `revit_primitives` : setup et helpers du shared param

`lib/revit_primitives.py` étendu d'un bloc « Shared parameter » :

- **Constantes** : `_SHARED_PARAM_GROUP_NAME = "claude-in-revit"`,
  `_SHARED_PARAM_NAME = "llm_id"`,
  `_SHARED_PARAM_GUID = "cca44e1c-7a8d-4b3e-9f50-7c1d8ab23e0a"`.
  GUID figé dans le code → un même `.rvt` ouvert sur n'importe quelle
  machine voit le même paramètre sans collision.
- **`_ensure_shared_params_file(path)`** : crée le fichier
  `~/.config/claude-in-revit/shared_params.txt` avec un header Revit
  valide (UTF-16 LE + BOM) si absent. Format minimal :
  `*META / META / *GROUP / *PARAM` — Revit complète lui-même quand on
  appelle `Definitions.Create(opts)`.
- **`_get_or_create_definition(doc)`** : définit `app.SharedParametersFilename`,
  ouvre via `OpenSharedParameterFile()`, get-or-create le groupe et
  la définition. Le `SpecTypeId.String.Text` est le type cible (équivalent
  ForgeTypeId de l'ancien `ParameterType.Text`).
- **`_all_bindable_categories(doc)`** : construit la `CategorySet` en
  itérant `doc.Settings.Categories` avec garde `try/except` sur
  `.AllowsBoundParameters` (certaines sous-catégories cachées
  raisent).
- **`ensure_shared_param_binding(doc)`** : orchestrateur idempotent.
  Early-return `False` si `ParameterBindings.Contains(definition)` —
  pas de Tx ouverte dans ce cas (important : safe to call from
  helpers déjà-inside-Tx). Sinon ouvre sa propre Tx Revit
  « claude-in-revit: bind llm_id shared param » et insère le binding
  avec `GroupTypeId.IdentityData` (donc affiché sous « Identification »).
- **`set_llm_id_on_element(element, llm_id) -> bool`** : écrit la
  valeur via `element.LookupParameter("llm_id").Set(...)`. Silent sur
  échec (returns False) — UX, jamais fatal.
- **`get_llm_id_from_element(element) -> Optional[str]`** : lecture
  fallback documentée comme telle. KG reste autorité en flow normal.

### Phase 4 — Plumbing : stamping dans les tools d'écriture

Helper centralisé `lib/tools/_helpers.py:stamp_llm_id(element, llm_id)` :
résolution via `getattr(rp, "set_llm_id_on_element", None)`, no-op si
la fonction est absente (cas tests hors-Revit avec stub
`revit_primitives`). Silent sur toute exception — c'est de la surface
UX, pas du data path.

Appels insérés après chaque `kg.set_revit_id(llm_id, revit_id)`,
toujours à l'intérieur du `rp.transaction(doc, ...)` ouvert par le
tool (param.Set nécessite une Tx Revit active) :

- `walls.py:walls_create` (solo).
- `walls.py:walls_create_many` (boucle bulk).
- `columns.py:columns_create` (solo).
- `columns.py:columns_create_many` (boucle bulk).

Pour les transforms (translate / rotate / mirror / copy / array_*),
le stamping est porté par `kg_sync.ingest_revit_element` via le helper
privé `_stamp_param_silent(element, llm_id)` (équivalent local au
`stamp_llm_id` mais dans `kg_sync` pour éviter une dépendance
ascendante `tools/ → kg_sync.py`). Les sept transforms en bénéficient
automatiquement sans modification de leur code.

### Phase 5 — Arrondi FP dans les convertisseurs

`kg_sync.py:_r(value) -> float` avec `_FP_PRECISION = 6`. Wrapper
appliqué dans :

- `_level_to_attrs` (elevation).
- `_wall_type_to_attrs` (total_thickness).
- `_wall_to_attrs` (p1, p2, length, height).
- `_column_to_attrs` (position, height).
- `_curve_element_to_attrs` (p1, p2, length).

Coût négligeable, JSON nettement plus lisible. Test e2e
(`test_full_rescan_persists_clean_floats_in_kg`) vérifie le round-trip
disque → load — `0.20000000000000004` revient bien en `0.2`.

### Phase 6 — Bug runtime : `BuiltInParameterGroup` removed in Revit 2025

Au premier clic Refresh KG post-livraison, `ImportError` sur l'import
de `BuiltInParameterGroup` (cf. Décision 8). Le traceback du shell
défensif a permis de localiser instantanément. Fix :

```python
# revit_primitives.py imports
- BuiltInParameterGroup,
+ GroupTypeId,

# ensure_shared_param_binding body
- bindings_map.Insert(definition, binding, BuiltInParameterGroup.PG_IDENTITY_DATA)
+ bindings_map.Insert(definition, binding, GroupTypeId.IdentityData)
```

Commentaire ajouté à côté du call site pour la traçabilité du switch.
La suite de tests hors-Revit n'avait pas pu détecter (le module
`revit_primitives` n'est pas importable sous pytest) — exactement le
genre de régression API qu'il faudra attraper soit par un linter sur
les imports Revit, soit par un test runtime systématique post-livraison.

### Phase 7 — Tests (9 nouveaux, total 161 → 170)

`tests/test_project_kg.py` (+4) :
- `test_clear_topology_preserve_counters_keeps_them` : counter reste
  à `Wall: 3` après `_clear_topology(preserve_counters=True)`, le
  prochain `add_node("Wall", …)` retourne `wall_004` (pas `wall_001`).
- `test_add_node_emit_log_false_suppresses_create_entry` : un
  `add_node(_emit_log=False)` ajoute le nœud mais pas d'entrée
  `create` dans `action_log`. Comparaison avec un `add_node` par
  défaut qui *ajoute* bien l'entrée.
- `test_snapshot_revit_id_map_returns_mapping_including_deleted` :
  vérifie qu'un nœud soft-deleted apparaît dans le snapshot (sa
  réapparition côté Revit doit pouvoir recouvrir son llm_id).
- `test_snapshot_skips_nodes_without_revit_binding` : nœud sans
  `set_revit_id` ne pollue pas le snapshot.

`tests/test_kg_sync.py` (+5) :
- Fixture `_install_rescan_stub(monkeypatch, levels=…)` qui injecte
  un `revit_primitives` minimal (`levels`, `wall_types`, …, `transaction`,
  `internal_to_meters`). Pas d'`ensure_shared_param_binding` / `set_llm_id_on_element`
  sur le stub → `getattr(rp, …, None)` retourne `None` → branche
  pure-KG exercée.
- `test_full_rescan_reuses_llm_id_when_revit_id_matches` : deux
  Levels avec revit_ids 100/200 pré-existants, plus un 3ᵉ (revit_id=300)
  inconnu. Après rescan : `level_001`/`level_002` conservés,
  `level_003` frais, `summary["preserved_llm_ids"] == 2`.
- `test_full_rescan_action_log_has_rescan_only_no_creates` : 3
  Levels scannés, action_log gagne **un seul** event de type
  `rescan` avec le bon summary. Aucun `create` ne fuit.
- `test_full_rescan_counter_advances_past_preserved_ids` : counter
  préservé travers `_clear_topology` → un id réutilisé `level_001`
  + un nouvel élément donne `level_002`, jamais `level_001`
  bis.
- `test_r_strips_feet_to_meters_artifacts` : `_r(0.20000000000000004)
  == 0.2`, `_r(4.999999999999992) == 5.0`, `_r(0.024999999999999998)
  == 0.025`. Précision micrométrique vérifiée (`1.234567891234` →
  `1.234568`).
- `test_full_rescan_persists_clean_floats_in_kg` : round-trip
  full_rescan → `ProjectKG.load` reste à `0.2`.

### Validation

- `pytest -q` → **170 passed en 5.64s** (161 → +9). Aucune régression.
- **Validation runtime** ✓ (confirmée par utilisateur) : un Refresh
  KG → des opérations de mutation → un 2ᵉ Refresh KG sur le même
  modèle conserve maintenant les `llm_id` (avant : renumérotation
  silencieuse). Le paramètre `llm_id` apparaît dans Propriétés sous
  « Identification » à côté de Mark sur les éléments cliqués.

### État final & reste à faire

**Acquis session 3** :
- `add_node(_emit_log)`, `_clear_topology(preserve_counters)`,
  `snapshot_revit_id_map` côté `ProjectKG` ✓
- `full_rescan` snapshot/reuse + log filtré + summary `preserved_llm_ids` ✓
- `ensure_shared_param_binding` + `set_llm_id_on_element` +
  `get_llm_id_from_element` côté `revit_primitives` ✓
- Plumbing `stamp_llm_id` dans walls/columns create + many ✓
- `_stamp_param_silent` dans `ingest_revit_element` → transforms
  couvertes ✓
- Arrondi FP à 1 µm dans tous les convertisseurs ✓
- Fix `BuiltInParameterGroup` → `GroupTypeId.IdentityData` (Revit 2025) ✓
- 9 nouveaux tests, baseline 170 verts ✓
- Validation runtime confirmée par utilisateur ✓

**Dettes / TODO créés** :

1. **Pas d'`IUpdater` pour hard-locker le param** — soft lock
   (re-stamp au rescan) suffit en V0, à reconsidérer si l'usage
   révèle des éditions manuelles fréquentes (V1+).
2. **Pas de test d'imports Revit hors process** — la régression
   `BuiltInParameterGroup` a été détectée au runtime. Possibilité :
   un linter custom qui valide les imports `Autodesk.Revit.DB.*`
   contre une liste curée par version de Revit. Pas urgent (la
   surface est petite).
3. **Anciens KG sur disque avant cette session** ont des
   `action_log` pollués de `create` redondants — pas de migration
   automatique, ils continuent à charger normalement (le code lit
   les events tel qu'écrits). Si un audit historique devient
   nécessaire, faire une passe `tools/cleanup_action_log.py`.

**Suite immédiate** :
- Reprendre Semaines 2-3 V0 (§9 DESIGN) : `openings.py`
  (create_door, create_window, set_sill_height, set_lintel_height)
  + convertisseurs Door/Window dans `kg_sync` ; `rooms.py` ;
  `levels.py` ; convertisseurs associés pour que le shared param
  llm_id stamp marche d'office sur les nouveaux types
  (binding déjà universel, cf. Décision 5).

---



### Contexte & objectif

Entrée rétro rédigée en session 3 du 2026-05-11 pour combler le journal
qui s'arrêtait à la Phase 12 (sélection active), alors que le commit
`a97d485 V0 Sem.1-2` (le même que les Phases 1-12) a aussi livré ~1850
lignes de code non documentées et a fait passer le compteur de tests
83 → 161. Le drift a été détecté pendant la validation runtime des
scénarios 1-4 quand `walls_create_many` et `catalog_list_walls` sont
apparus dans l'historique LLM sans entry journal correspondante.

Trois axes structurent les ajouts :
- **Nouveaux types BIM** dans le schéma KG : `Column` / `ColumnType`
  (architectural vs structural), `ModelLine` / `DetailLine`.
- **Multi-objets** : tools `create_many` qui prennent une liste
  explicite d'items et font N créations en une seule transaction.
- **Paramétriques** : tools qui *génèrent* N items depuis un petit jeu
  de paramètres (`walls_create_polyline`, `columns_create_grid`,
  `columns_create_grid_irregular`, `walls_create_from_lines`,
  `elements_array_*`). Économie tokens en plus de l'ergonomie LLM.
- **Optimisations tokens** sur la communication avec Claude :
  `bulk_summary` (~125× sur les réponses bulk), `compact_error` (~95%
  sur `is_error`), split system prompt STATIC/DYNAMIC pour cache hit
  stable, `trim_history` fenêtre glissante 30K, attache 1 MB
  image/PDF/text.

### Décisions

1. **Trois patterns pour la création en masse**, distincts par
   sémantique d'entrée :
   - `*_create_many(items=[...])` : liste explicite d'items
     individuels — appelé par le LLM quand chaque mur/poteau a sa
     géométrie propre.
   - `*_polyline(vertices, …)` / `*_grid(origin, step, count, …)` /
     `*_grid_irregular(origin, x_spacings, y_spacings)` : génération
     paramétrique — appelé quand la régularité de la structure se
     décrit en quelques chiffres. Implémenté en *adapter* qui calcule
     la liste d'items et délègue à `*_create_many` (ne duplique pas la
     transaction / validation).
   - `*_from_lines(line_llm_ids, …)` : pont entre types — convertit
     des `ModelLine` / `DetailLine` existantes en murs. Adapter sur
     `walls_create_many` également.
   Pourquoi trois : minimise les tokens LLM côté input (un
   `grid` 5×5 = 8 paramètres vs 25 items), tout en gardant un fallback
   explicite pour les configurations irrégulières.

2. **`bulk_summary` factorisé** dans `lib/tools/_helpers.py` plutôt que
   répété dans chaque tool. Compaction : `count ≤ 8` → liste explicite
   d'ids ; `count > 8` ET ids contigus → `{first_llm_id, last_llm_id,
   contiguous: true}` ; sinon → liste + note. Bénéfice mesuré : un
   `array_linear(count=50)` passe de ~3000 tokens (50 ids énumérés
   avec revit_ids) à ~24 tokens. Le LLM peut référencer par range
   (`wall_017..wall_066`) sur le tour suivant — la sémantique
   contigus est préservée par les counters typés du KG.

3. **`compact_error`** dans `lib/llm_protocol.py:_compact_tool_error`.
   PythonNet wrappe les exceptions .NET en messages multi-lignes
   massifs (5000+ tokens vu sur les erreurs Revit). On cape à
   `type: msg[:400]` + suffix `…[truncated]`. Le traceback complet
   reste capturé par le shell défensif du pushbutton — pas perdu, juste
   pas envoyé au LLM. Décision : le LLM n'a besoin que du type + début
   du message pour itérer correctement, le reste pollue.

4. **System prompt split STATIC / DYNAMIC** dans
   `prompt.pushbutton/script.py`. Bloc 0 = instructions universelles
   (rôle, conventions llm_id, comportement attendu) marqué
   `cache_control: ephemeral` ; bloc 1 = état courant (project_id,
   turn, counts KG, sélection active) sans cache. Avant le split, la
   moindre variation tournique invalidait tout le préfixe. Avec le
   split, le préfixe statique se cache à partir du 2ᵉ tour et le
   dynamic se ré-encode pour ~50-100 tokens. Aligné §7 du DESIGN doc.

5. **`trim_history` à fenêtre glissante 30K tokens** (~120K chars,
   heuristique `_approx_chars` qui marche les structures sans
   `json.dumps`). Drop entrées du front, garde min 2 dernières pour
   contexte, puis `sanitize_history` pour purger les `tool_use`
   orphelins (sans `tool_result` matching) qui plantent l'API. Plus
   pragmatique que la décision §7 du DESIGN (trim après 3 tours
   fixés) : on conserve l'historique tant qu'il rentre, on émonde au
   plus tard.

6. **`transforms.py` type-agnostique** : les 7 tools acceptent des
   `llm_ids` hétérogènes (Wall, Column, Line) parce qu'`ElementTransformUtils`
   en API Revit ne discrimine pas le type. Le post-traitement KG passe
   par `ingest_revit_element(element)` (`kg_sync.py`), dispatcher qui
   détecte le type Revit via `isinstance` et appelle le convertisseur
   approprié. **Conséquence** : ajouter un nouveau type supporté
   (Door, Window, Beam…) ne demandera *aucun* nouveau tool transforms,
   uniquement un convertisseur Element→attrs.

7. **Contournement Revit 2025 `CopyElements(Transform composé)`** :
   l'API rejette en silence un `Transform` qui compose translation +
   rotation. Décision : split en deux phases — Phase A
   `CopyElements(XYZ)` (overload translation pure), Phase B
   `RotateElements` in-place autour du pivot translaté. Pattern
   appliqué à `elements_copy` et `elements_array_parametric`.
   Commentaire explicite dans le code à chaque endroit pour ne pas
   refaire l'erreur si quelqu'un repasse en `Transform` composé en
   pensant simplifier.

8. **`OST_Lines` via `OfClass(CurveElement) + isinstance`** plutôt que
   filtre `OfCategory(OST_Lines)` direct. Raison : le filtre catégorie
   ne matche pas toujours les `ModelLine` selon la version Revit. La
   double-passe (collector large + filtre Python) est plus robuste.
   Documenté dans la docstring de `_curve_elements()` (`kg_sync.py`).

9. **`Column.kind` (architectural | structural) déterminé par
   `Category.Id`** : `OST_Columns` → architectural, `OST_StructuralColumns`
   → structural. Posé sur `ColumnType` à l'ingest puis propagé à
   l'instance. Pas de mutation possible après création — un mur ne
   change pas de catégorie en Revit, un poteau non plus. Le champ
   `kind` est requis côté `columns_create` pour choisir le bon
   `StructuralType` à passer à `NewFamilyInstance`.

10. **Hauteur de poteau via `FAMILY_TOP_LEVEL_OFFSET_PARAM` + même
    top level que base** plutôt que par top-level supérieur. Choix
    pragmatique : permet une "unconnected height" indépendante de la
    présence d'un niveau au-dessus, donc fonctionne sur un projet
    mono-niveau (cas du test runtime).

### Phase 13 — Walls multi-objets et paramétriques

`lib/tools/walls.py` étendu de 3 tools :

- `walls_create_many(items)` — N murs, 1 transaction Revit + 1
  transaction KG. Validation upfront (`_validate_wall_item`) sur tous
  les items avant la moindre mutation : atomicité garantie. Retourne
  `bulk_summary(llm_ids)`.
- `walls_create_polyline(vertices, height?, closed?)` — chaîne de
  murs entre sommets. Adapter qui calcule la liste d'items et délègue
  à `walls_create_many`. `closed=True` ajoute le segment retour.
- `walls_create_from_lines(line_llm_ids, height?)` — convertit des
  `ModelLine` / `DetailLine` du KG en murs. Drop de la composante z
  (les murs sont 2D dans le plan du niveau, signalé dans la docstring).
  Adapter sur `walls_create_many`.

Hauteur par défaut : `_default_story_height(kg, level_ref)` lève si
le niveau est le topmost — pas de devinette silencieuse, l'agent doit
fournir `height` explicitement.

### Phase 14 — Module `columns.py` (nouveau type)

Schéma KG étendu (`project_kg.py NODE_TYPES`) :
- `ColumnType` : `family_name`, `type_name`, `kind`.
- `Column` : `level_ref`, `type_ref`, `position [x,y]`, `height`, `kind`.
- Edges `at_level` (Column → Level), `is_type` (Column → ColumnType)
  — réutilise les edges existants, pas de nouveau type.

Tools livrés :
- `catalog_list_column_types()` / `catalog_list_columns()`.
- `columns_create(level_ref, column_type_ref, position, height?)` :
  solo, branche Revit + branche KG-only. Validation
  `FAMILY_TOP_LEVEL_*` params (lève si la famille ne les expose pas).
- `columns_create_many(items)` : batch avec activation préalable des
  `FamilySymbol` inactifs (`_activate_symbols`) pour éviter une
  regenération par `NewFamilyInstance` — critique pour grilles 20+
  poteaux.
- `columns_create_grid(origin, step_x, step_y, count_x, count_y, …)`
  et `columns_create_grid_irregular(origin, x_spacings, y_spacings, …)` :
  paramétriques, calculent les positions localement, délèguent à
  `columns_create_many`.

### Phase 15 — Module `transforms.py` (7 tools type-agnostiques)

`lib/tools/transforms.py` (~810 lignes) :

| Tool | Action | Notes |
|------|--------|-------|
| `elements_translate(llm_ids, vector)` | in-place | `MoveElements`, KG refresh via `_refresh_kg_geometry` |
| `elements_rotate(llm_ids, center, angle_deg)` | in-place | Pas de branche KG-only — requiert Revit |
| `elements_mirror(llm_ids, plane_origin, plane_normal)` | copie | Retourne les llm_ids des copies |
| `elements_copy(llm_ids, translation, rotation_angle_deg?, rotation_center?)` | 1 copie | **Split Phase A/B** Revit 2025 |
| `elements_array_linear(llm_ids, vector, count)` | N-1 copies | Original + 1..N-1 vector |
| `elements_array_rotational(llm_ids, center, total_angle_deg, count)` | polaire | `Transform.CreateRotationAtPoint` (rotation pure → accepté par CopyElements) |
| `elements_array_parametric(src_llm_ids, count, per_step_translation?, per_step_rotation_deg?, rotation_center_mode?, per_step_shortening_m?)` | composé | Phase A translation, Phase B rotation, Phase C shortening |

`_shorten_element_endpoints` (helper) : mute `LocationCurve.Curve`
(Walls) ou `GeometryCurve` (ModelCurve/DetailCurve), no-op pour
Columns, lève si shortening > demi-longueur.

`_refresh_kg_geometry(kg, llm_ids, doc)` : après transform, relecture
des attrs géométriques depuis Revit pour chaque llm_id. Refs
(level_ref, type_ref) inchangées.

### Phase 16 — `kg_sync` étendu + `ingest_revit_element` dispatcher

`lib/kg_sync.py` :

- Convertisseurs ajoutés :
  - `_column_type_to_attrs(symbol)`.
  - `_column_to_attrs(column, *, level_ref, type_ref)`.
  - `_curve_element_to_attrs(curve_element)` — endpoints 3D + length.
    Lève sur Arc/spline (Line only V0).
- `full_rescan` étendu de 3 → 7 passes :
  Levels → WallTypes → Walls → ModelLines → DetailLines → ColumnTypes
  → Columns. Tout dans `kg.transaction()` (atomicité préservée),
  try/except per-élément, summary élargi à
  `{levels, wall_types, walls, model_lines, detail_lines,
  column_types, columns, skipped: {...}}`.
- `ingest_revit_element(element) -> attrs` : dispatcher générique
  utilisé par les transforms après copie. `isinstance(Wall)` →
  `_wall_to_attrs` ; `isinstance(FamilyInstance)` + category check →
  `_column_to_attrs` ; `isinstance(ModelCurve | DetailCurve)` →
  `_curve_element_to_attrs` ; autre → `ValueError` explicite.

`revit_primitives.py` : helpers `model_lines(doc)`, `detail_lines(doc)`,
`column_types(doc)`, `columns(doc)` ajoutés ; pattern `OfClass` +
`isinstance` documenté.

### Phase 17 — `_helpers.py` : `bulk_summary` + `compact_error`

`lib/tools/_helpers.py` (nouveau) :
- `_is_contiguous_llm_ids(llm_ids)` : préfixe commun + suffixes ints
  contigus.
- `bulk_summary(llm_ids, small_threshold=8)` : voir Décision 2.

Utilisé par `walls_create_many` / `_polyline` / `_from_lines`,
`columns_create_many` / `_grid` / `_grid_irregular`, et les 7
`elements_*` de `transforms.py`.

`lib/llm_protocol.py:_compact_tool_error` : voir Décision 3.

### Phase 18 — Optimisations tokens runtime (system prompt + history)

`lib/llm_api.py` et `prompt.pushbutton/script.py` :

- **System prompt split STATIC / DYNAMIC** : voir Décision 4. Le
  block STATIC sort de l'inline du pushbutton et tagué
  `cache_control: ephemeral` ; le DYNAMIC se construit par turn avec
  l'état courant (project_id, turn count, KG node counts, sélection
  active formatée par `_format_selection_line`).
- **`trim_history_to_max_chars`** : voir Décision 5. Algorithme :
  drop entrées du front jusqu'à `total ≤ max_chars`, garde min 2
  dernières, puis `sanitize_history` (purge `tool_use` orphelins).
  Retourne `(trimmed, dropped_count)`.

### Phase 19 — Catalogues étendus + `query_get_node`

`lib/tools/catalog.py` + `lib/tools/columns.py` exposent :
- `catalog_list_walls()` — `{walls: [{llm_id, level_ref, type_ref,
  p1, p2, length, height}, …]}`.
- `catalog_list_lines()` — `{lines: [{llm_id, kind, p1, p2, length},
  …]}` (kind = `ModelLine` | `DetailLine`).
- `catalog_list_column_types()`, `catalog_list_columns()`.

`lib/tools/query.py` :
- `query_get_node(llm_id)` : lit tous les attrs d'un nœud KG quel que
  soit son type. Pivot LLM générique vs ajouter un tool getter par
  type.

### Phase 20 — UI WinForms 5 lignes + attache 1 MB

`prompt.pushbutton/script.py` :
- Remplace l'`InputBox` VB6 (Phase 6 du journal initial) par une
  **Form WPF custom** (~140 lignes) : Label question, TextBox
  multiline 5 lignes (Ctrl+Enter = submit, Esc = cancel), bouton
  attach + label statut, checkbox reset history, OK/Cancel.
- Attachment picker : `OpenFileDialog` avec filtre personnalisé.
  Validation locale `os.path.getsize` ≤ 1 MB, refus avec message
  actionnable au-delà.
- Types reconnus :
  - Images PNG/JPEG/GIF/WEBP → content block `type: "image"`,
    `source: base64`.
  - PDF → `type: "document"`, `source: base64`,
    `media_type: application/pdf`.
  - Texte (.txt, .md, .csv, .json, .py, .log, …) → inliné comme
    block `type: "text"` avec délimiteur.
- `lib/llm_api.py:build_user_content(prompt, attachment_path)`
  retourne soit une string (pas d'attachement) soit une liste de
  content blocks (avec attachement). Garde la signature simple côté
  caller.
- Checkbox reset : si cochée → `history = []` avant `run_turn`,
  persisté immédiatement → conversation fresh mais KG préservé.

### Validation

- `pytest -q` : **161 tests verts en ~6 s** (83 → +78). Aucune
  régression sur la baseline Phases 1-12.
- Validation runtime déjà couverte par les scénarios de validation
  (cf. messages utilisateur du 2026-05-11 20:34 : `walls_create`,
  `walls_create_many`, `walls_set_height` ×4 en parallèle,
  `walls_move`, `walls_delete`, multi-turn avec récupération
  d'erreur sur `Unknown llm_id`).

### État final & reste à faire

**Tools livrés au total après cet addendum (V0 Sem.1-2)** :

- **Walls** (7) : `create`, `delete`, `move`, `set_height`,
  `create_many`, `create_polyline`, `create_from_lines`.
- **Columns** (5) : `create`, `create_many`, `create_grid`,
  `create_grid_irregular` + 2 catalogues.
- **Lines** (lecture seule V0) : `catalog_list_lines`, ingestion
  ModelLine/DetailLine au rescan.
- **Transforms** (7) : `translate`, `rotate`, `mirror`, `copy`,
  `array_linear`, `array_rotational`, `array_parametric`.
- **Catalogues / Query** : `catalog_list_levels`, `_wall_types`,
  `_walls`, `_lines`, `_column_types`, `_columns` ; `query_get_node`,
  `query_find_by_name`.
- **Aggregations** : `aggregations_count` (héritage slice).

**Dettes / TODO identifiées dans le code** :

1. Schéma KG `Door` / `Window` / `Room` / `Compartment` déclarés mais
   non peuplés au `full_rescan` (Semaines 2-3 V0 du DESIGN doc).
2. Murs courbés (`Arc` LocationCurve) tombent sur les endpoints de la
   chord (`kg_sync.py` l. 368). À traiter avec la modification de
   walls courbés.
3. `DetailLine` ingéré sans son binding `View` (le KG ne sait pas
   dans quel plan/coupe la ligne vit). À fixer quand `View` deviendra
   un node type.
4. Persistance d'`_revit_id` à travers les sessions Revit non
   stampée — pas de session-id mismatch detection (V0 accepte qu'un
   rescan résorbe d'un clic).
5. **Bug UX confirmé par validation runtime** : les `llm_id` ne sont
   pas visibles dans Revit ET sont **réassignés au `full_rescan`**.
   Conséquence : l'historique LLM contient des `llm_id` fantômes
   après un rescan (`wall_006` disparaît, `wall_005` devient un autre
   mur). Trou design critique pour les flows multi-tour — fixé en
   session 3 du 2026-05-11 (voir entrée séparée).
6. CPython pip bootstrap : documenté dans CLAUDE.md, pas automatisé.

**Suite immédiate** — fixé en session 3 du 2026-05-11 (voir entrée
séparée) :
- Fix `llm_id` stable au rescan via snapshot `revit_id → llm_id` côté
  KG + UX miroir sur Shared Parameter Revit `claude-in-revit:llm_id`.
- Filtrage des entrées `create` redondantes dans `action_log` quand
  l'écriture vient d'un `full_rescan` (seul l'event `rescan` avec
  son summary subsiste).
- Arrondi FP des convertisseurs `kg_sync` à 6 décimales (1 µm).

**Semaines 2-3 V0 (§9 DESIGN)** — toujours à faire :
- `openings.py` : create_door, create_window, set_sill_height,
  set_lintel_height + convertisseurs Door/Window.
- `rooms.py` : create, recompute_boundaries, set_name, get_area +
  convertisseur Room.
- `levels.py` : create, modify_elevation, set_active.

---

## 2026-05-11 (session 2) — `kg_sync.py` + `@kg_synced` + extension de `project_kg`

### Contexte & objectif

Item #1 du « reste à faire » de la session 1 du jour : poser `kg_sync.py`,
le morceau critique de la Semaine 1 V0 (§9 du design doc), qui fait le
pont entre `revit_primitives.py` (collecteurs Revit bruts) et
`project_kg.py` (schéma KG typé). Trois sous-livrables : binding KG ↔
Revit, `full_rescan(doc, kg)`, décorateur `@kg_synced(name)` qui apparie
les deux transactions §4.1.

Pas de Revit ouvert cette session — toute la validation est faite hors
process. Les chemins Revit-réels (`full_rescan` qui scrute le doc,
`revit_id_of` qui construit un `ElementId`) sont exercés à la prochaine
session quand on cliquera sur `refresh_kg.pushbutton`.

### Décisions

1. **`_revit_id` comme attr réservé du KG** (au même rang que `_type`,
   `created_at_turn`, `deleted_at_turn`). Posé via `kg.set_revit_id(llm_id,
   element_id)` qui **bypass la validation schéma** par node type, à
   l'image de `soft_delete` qui pose `_deleted_at_turn` sans passer par
   `modify_node`. Évite d'ajouter `revit_id` aux `optional` de chaque
   entrée de `NODE_TYPES` (10+ types à terme — bruit de plomberie).
   `_revit_id` ride along dans le roundtrip `to_dict`/`from_dict` sans
   modif puisque les attrs nœud sont sérialisés génériquement.

2. **`full_rescan` hybride** — choisi par l'utilisateur après proposition
   en trois variantes. Vide nœuds/edges/counters, **garde `turn` et
   `action_log`**. Justification : la timeline conversationnelle ne doit
   pas être coupée par un refresh en milieu de session ; `diff_since()`
   continue à fonctionner. Une seule entrée `rescan` est appendée au
   log (vs N entrées `create` qui pollueraient pour rien).

3. **Ordre des transactions imbriquées** : KG externe, Revit interne.
   Justification : le snapshot KG est pris avant que Revit ne touche le
   document. Sur exception, l'inner `revit_primitives.transaction`
   rollback Revit en premier, puis l'outer `kg.transaction()` restore
   le snapshot. La séquence inverse (Revit externe) laisserait une
   fenêtre où `kg.persist()` peut écrire un état que Revit refuse ensuite
   au commit.
   *Drift résiduel accepté* : `kg.persist()` échoue après le commit
   Revit — Revit a la donnée, le disque pas. Couvert par le bouton
   `refresh_kg` (§10 mitigation KG drift), pas par du 2PC.

4. **ElementId persisté dans le JSON** malgré la note REVIT_API_NOTES
   « ne pas persister un ElementId entre sessions Revit ». Trade-off V0
   conscient : `full_rescan` est idempotent → un mismatch session-vs-disque
   est résolu en un clic refresh. Si le mismatch devient un problème
   réel, ajout d'un session-id stamp + invalidation auto à charger ;
   pas la peine d'over-engineer maintenant.

5. **Lazy imports dans `kg_sync.py`** — top-level n'importe pas
   `revit_primitives` ni `Autodesk.Revit.DB`. Chaque fonction
   Revit-touching fait son `from . import revit_primitives as rp`
   localement. Conséquence : le module est importable sous pytest, et
   les tests sur `bind`/`llm_id_of`/`@kg_synced` peuvent
   `monkeypatch.setitem(sys.modules, "lib.revit_primitives", stub)` au
   lieu de devoir installer PythonNet + RevitAPI.dll.

### Phase 1 — Extension de `project_kg.py`

Trois ajouts :

- `REVIT_ID = "_revit_id"` ajouté à `_RESERVED_ATTRS`.
- `set_revit_id(llm_id, revit_id) / get_revit_id(llm_id) /
  find_by_revit_id(revit_id)` posent et lisent directement
  `node[REVIT_ID]`, KeyError si nœud inconnu, lookup linéaire O(N) sur
  l'inverse (acceptable pour les tailles cibles, §10 risque NetworkX).
- `_clear_topology()` privé pour `full_rescan` : reset
  `_g = MultiDiGraph()` et `_counters = {}`, mais ne touche pas à
  `_turn`, `_action_log`, `project_id`, `persist_path`. Sémantique
  hybride décidée plus haut.

### Phase 2 — `lib/kg_sync.py`

Trois groupes :

- **Helpers stateless** (`_extract_revit_id`, `bind`, `revit_id_of`,
  `llm_id_of`). `_extract_revit_id` accepte `Element` (via `.Id`),
  `ElementId` (via `.Value` ou `.IntegerValue` pre-2024), ou int brut.
  C'est la couche qui couvre le breaking change 2024 sur ElementId
  noté dans REVIT_API_NOTES — un seul endroit à mettre à jour si
  on monte en version Revit ou si Autodesk recasse ce contrat.
- **Convertisseurs Element → attrs** (`_level_to_attrs`,
  `_wall_type_to_attrs`, `_wall_to_attrs`). Conversions m↔feet ici
  pour que le KG ne voie jamais d'unités internes Revit. `_wall_to_attrs`
  lit `WALL_USER_HEIGHT_PARAM` (cf. REVIT_API_NOTES § Phase 2) ; les
  murs courbés tombent sur les endpoints de la chord (TODO documenté
  pour la phase géométrie).
- **`full_rescan(doc, kg)`** scanne dans l'ordre Level → WallType →
  Wall (les Walls dépendent des deux premiers via `at_level` et
  `is_type`). Les walls dont le `LevelId` ou le `WallType` n'est pas
  mappable (lien externe, catégorie filtrée) sont **skip silencieusement**
  plutôt que d'inventer des refs — quand on touchera l'UI on remontera
  ces skip dans le summary.
- **`@kg_synced(name_or_fn)`** supporte les deux formes (`@kg_synced` et
  `@kg_synced("nom")`). Lazy import de `revit_primitives` *dans le
  wrapper*, pas à la décoration, pour rester testable.

### Phase 3 — Tests

5 tests ajoutés à `tests/test_project_kg.py` : roundtrip
`set/get_revit_id`, KeyError sur nœud inconnu, reverse lookup, survie
persistence, `_clear_topology` qui préserve turn + log.

10 tests dans `tests/test_kg_sync.py` :

- 4 sur `_extract_revit_id` (int, ElementId via `.Value`, Element via
  `.Id.Value`, fallback `.IntegerValue` pre-2024).
- 2 sur `bind` + `llm_id_of` (accepte un objet Element-like, reverse
  lookup retourne None si non-mappé).
- 4 sur `@kg_synced` : commit/persist OK ; rollback symétrique sur
  exception dans le body ; forme bare `@kg_synced` sans args (utilise
  `fn.__name__`) ; commit-time failure (raised par la transaction Revit
  mockée *après* le yield) ⇒ KG snapshot quand même restauré.

Fixture `fake_revit_primitives` qui injecte un `types.ModuleType` dans
`sys.modules["lib.revit_primitives"]` avec une `transaction()`
trackante. Le lazy import dans `_wrap` ramasse le stub sans difficulté.

### Validation

- `pytest -q` → **50 passed en 0.97s** (35 baseline + 15 nouveaux).
  Aucune régression sur les tests existants malgré la modif de
  `project_kg.py` (ajout dans `_RESERVED_ATTRS`, trois nouvelles
  méthodes, un nouveau helper privé).
- Pas de test Revit-réel cette session. À faire à la prochaine ouverture
  de Revit : clic sur `Refresh KG` doit (i) ne plus afficher le
  TaskDialog « not implemented yet », (ii) appeler
  `kg_sync.full_rescan(doc, kg)` et afficher le summary
  `{levels, wall_types, walls}`.

### Phase 4 — Câblage `refresh_kg.pushbutton` + identifiant projet

Continuation directe : item #1 du reste à faire devient livrable dans la
foulée. Permet de boucler la chaîne « clic bouton → rescan Revit → KG
persisté sur disque » bout-en-bout pour la prochaine session Revit.

**Décision identifiant projet** : on prend la branche fallback du §8
seule (hash 16-hex de `doc.PathName`), sans la tentative #1 (param Revit
partagé `claude-in-revit.project_uuid`). Justification : créer un
shared parameter file, le binder à `ProjectInformation`, gérer le
roundtrip de la valeur est substantiel — pas justifié tant qu'on n'a
pas un cas d'usage où `Save As` orphelinerait un KG existant.
Documenté dans la docstring de `project_id_for` ; à reconsidérer si on
hit ce cas en pratique. Document unsaved → fallback sur `doc.Title`
préfixé `"title:"` pour éviter une collision théorique avec un PathName
qui contiendrait littéralement « title:Sandbox ».

**`lib/config.py`** : ajout de `projects_dir()` et `kg_path_for(project_id)`.
Constantes `PROJECTS_SUBDIR="projects"`, `KG_FILE_SUFFIX=".kg.json"` —
alignées avec §8. Pas d'auto-création du dossier ici : c'est
`ProjectKG.persist()` qui le fait au premier write via
`mkdir(parents=True, exist_ok=True)`.

**`lib/kg_sync.py`** : deux helpers ajoutés en haut du module (avant les
binding helpers).

- `project_id_for(doc)` : SHA-256 de `doc.PathName.strip()` (16 hex
  chars), fallback `"title:<Title>"` si pas sauvé. SHA-256 truncated
  est universellement dispo, deterministe, et 64 bits d'entropie sont
  largement suffisants pour la taille de portefeuille cible.
- `open_or_create(doc)` : `ProjectKG.load(path)` si le fichier existe,
  sinon `ProjectKG(project_id, persist_path=path)`. Le KG retourné a
  toujours `persist_path` peuplé (la dette du 2026-05-10 est gérée par
  la construction explicite — `load()` passe déjà `persist_path` via
  `from_dict`, donc la dette est résorbée).

**`refresh_kg.pushbutton/script.py`** : enlève le placeholder
TaskDialog, ajoute le path-fixup `sys.path` (même pattern que
`prompt.pushbutton`), récupère `doc` via `__revit__.ActiveUIDocument`
avec garde contre `None` (cas « aucun projet ouvert »), `open_or_create`
+ `full_rescan`, affiche le summary `{levels, wall_types, walls}` +
le `project_id` + le `persist_path`. Wrappé en `try/except` parce que
les erreurs Revit lors du rescan ne doivent pas laisser pyRevit
afficher son traceback technique — un TaskDialog lisible suffit pour
l'instant.

### Tests Phase 4

2 tests ajoutés à `test_config.py` :
- `projects_dir()` résolvent bien sous `fake_home`.
- `kg_path_for("abc123")` produit `…/projects/abc123.kg.json`.

5 tests ajoutés à `test_kg_sync.py` (avec fixture `fake_home` locale
qui monkeypatche `Path.home`) :
- `project_id_for` déterministe pour le même PathName, indépendant du
  Title.
- Deux PathName différents → ids différents.
- Doc unsaved → fallback sur Title, et l'id reste cohérent malgré du
  whitespace dans PathName.
- `open_or_create` sans fichier → KG vide avec `persist_path` set,
  rien sur disque tant qu'on ne persiste pas.
- `open_or_create` avec fichier existant → KG chargé, turn et nœuds
  restaurés.

Fixture `FakeDoc` minimal — juste `PathName` + `Title`. Le doc Revit
réel n'est pas importable hors-Revit, et `project_id_for` /
`open_or_create` ne lisent rien d'autre.

### Validation Phase 4

- `pytest -q` → **57 passed en 0.99s** (50 → +7 nouveaux). Aucune
  régression.
- Validation runtime à venir : clic « Refresh KG » dans Revit doit
  afficher le summary chiffré et écrire le fichier
  `~/.config/claude-in-revit/projects/<id>.kg.json`.

### Phase 5 — Vendoring de `networkx` (bug `No module named 'networkx'`)

Premier clic sur « Refresh KG » dans Revit : `ImportError: No module
named 'networkx'` au niveau de `lib/project_kg.py:17` (lui-même tiré
par `lib/kg_sync.py` chargé depuis le pushbutton).

**Cause racine** : le CPython embarqué de pyRevit
(`%APPDATA%\pyRevit-Master\bin\cengines\CPY3123\python.exe`) est une
**distribution embeddable** — son `python312._pth` désactive `site`
par défaut (`import site` commenté), donc pas de `site-packages`, pas
de pip d'office, stdlib uniquement. La `.venv/` locale a bien networkx
mais elle n'est pas exposée au runtime pyRevit. Pas un bug de notre
code, un trou dans le bootstrap d'environnement runtime.

**Trois options évaluées avec l'utilisateur** :

1. Bootstrap pip dans le CPython embarqué (décommenter `import site`
   dans `python312._pth`, get-pip.py, `pip install networkx`).
   Per-machine setup, élimine le poids vendoring.
2. **Vendor networkx dans le repo** sous `lib/_vendor/`. Zero-setup,
   bumps la taille (~13 MB). Choisi.
3. Remplacer networkx par un wrapper maison (~50 lignes). Élimine la
   dépendance, mais on devra la réintroduire en V1 pour les primitives
   compliance (Dijkstra fuite incendie §10).

**Décision : vendoring.** networkx 3.6.1 est pure-Python, zéro
dépendance runtime (`Requires-Dist` vide hors extras). 10.85 MB sur
disque. Copié de
`.venv\Lib\site-packages\networkx` → `claude-in-revit.extension\lib\_vendor\networkx\`
via PowerShell `Copy-Item -Recurse`.

**Bootstrap dans `lib/__init__.py`** : à l'import du package `lib`,
append (pas insert) `lib/_vendor/` à `sys.path`. Append plutôt que
insert pour qu'en local le venv résolve en premier — le lockfile
pyproject reste autoritative en dev, le `_vendor/` n'est qu'un
fallback runtime pour pyRevit.

**Setuptools** : `pyproject.toml` reçoit
`exclude = ["lib._vendor*"]` dans `[tool.setuptools.packages.find]`
pour ne pas embarquer networkx dans le wheel `claude-in-revit`. Le
fait que `_vendor/` n'ait pas d'`__init__.py` (de toute façon) suffirait
en pratique, mais l'exclusion explicite documente l'intention.

**Documentation** ajoutée dans `CLAUDE.md` (nouvelle ligne dans la
section pyRevit gotchas CPython, avec contrainte « pure Python
uniquement » et mention de l'alternative pip-in-embedded) et `README.md`
(architecture étendue, paragraphe sur le rationale).

### Validation Phase 5

- `pytest -q` → **57 passed en 0.89s**, aucune régression. Le venv
  continue à résoudre networkx depuis `site-packages`, le vendoring
  est transparent en dev.
- Sanity check du fallback : `python` avec `site-packages` retiré du
  sys.path → `import lib; import networkx` résout vers
  `…/lib/_vendor/networkx/__init__.py`. Reproduit l'environnement
  pyRevit en miniature.
- Validation runtime à venir : reclic sur « Refresh KG » dans Revit
  doit cette fois afficher le summary (au lieu de l'ImportError).

### Phase 6 — Durcissement runtime du pushbutton + politique Save-first

Deuxième clic sur « Refresh KG » après le vendoring : **NRE générique
Revit** (« Object reference not set to an instance of an object »), et
sur un projet vide une « erreur de stream » remontée par l'utilisateur.
La NRE fuit *par-dessus* notre `try/except Exception` initial — soit
elle surgit avant le try, soit l'exception .NET ne subclasse pas
`Exception` selon le wrap PythonNet courant.

Trois durcissements en réponse :

1. **`full_rescan` atomique + try/except par élément.** Le scan est
   maintenant enveloppé dans `kg.transaction()` (snapshot pris avant
   `_clear_topology`, restauration sur exception → jamais de KG
   à moitié vidé). Chaque conversion `Level/WallType/Wall` tourne dans
   un `try/except Exception` isolé : un curtain wall avec `Width = 0`,
   un wall sans `LocationCurve`, un type filtré non-mappable, etc.
   incrémentent `skipped[<type>]` au lieu de tuer le scan. Le summary
   porte maintenant `{"levels", "wall_types", "walls", "skipped":{...}}`
   et le TaskDialog l'affiche si non-zéro. Persistance déléguée à
   `transaction.__exit__()` (suppression du `kg.persist()` explicite).

2. **`refresh_kg.pushbutton` defensive shell.** Restructure en
   `_main()` appelé sous un `try / except BaseException`, avec
   `traceback.format_exc()` injecté dans le TaskDialog d'erreur.
   `BaseException` plutôt qu'`Exception` parce que PythonNet wrappe
   parfois les .NET exceptions hors-hiérarchie Python `Exception`.
   Imports déplacés *à l'intérieur* du try (un `ImportError` doit
   s'afficher en clair, pas comme NRE Revit). Gardes explicites sur
   `__revit__` (via `globals().get(...)`), `ActiveUIDocument`,
   `Document` — chacun produit un message diagnostic distinct.

3. **Politique Save-first** (validée par l'utilisateur). Si
   `doc.PathName == ""`, le pushbutton refuse avec un message qui
   pointe vers Fichier → Enregistrer sous. Justification : le
   fallback Title-based de `project_id_for` est techniquement
   fonctionnel mais (a) l'id migre au moment du Save (orphelinage
   du KG sur disque), (b) tous les brouillons Revit s'appellent
   `Project1` par défaut → collisions probables, (c) §8 du design
   doc fait du PathName l'identifiant canonique. Politique
   strictement plus simple à raisonner pour V0.

### Validation Phase 6

- `pytest -q` → **57 passed en 0.92s**. `full_rescan` n'est pas testé
  directement (Revit-only) mais la nouvelle signature et le wrap dans
  `kg.transaction()` ne cassent rien des chemins testés.
- Validation runtime à venir : trois scénarios à tester en Revit :
  (a) clic sans projet ouvert → message « aucun document actif »,
  (b) clic sur brouillon non sauvé → message « sauvegarde d'abord »,
  (c) clic sur projet sauvé même vide → summary avec compteurs
  (probablement 2 levels par défaut, 1-2 wall types template, 0 walls).
  Le cas qui produisait la NRE et l'erreur de stream doit maintenant
  remonter un traceback Python lisible dans le TaskDialog rouge si
  reproduit.

### Phase 7 — Bug `__revit__` invisible via `globals().get(...)`

Après `pyrevit caches clear 2025` + restart Revit, le defensive shell
de la Phase 6 a fait son boulot : on a un TaskDialog rouge avec
**Python traceback complet** au lieu de la NRE Revit générique. Diagnostic :

```
RuntimeError: __revit__ global not available — pyRevit didn't inject
the UIApplication into this script's globals.
  File "<string>", line 117, in <module>
  File "<string>", line 51, in _main
```

**Cause racine** : sous CPython, pyRevit injecte `__revit__` dans le
*namespace de résolution de noms* (built-ins ou équivalent), pas dans
le `dict` `globals()` du module. Donc `globals().get("__revit__")`
retourne `None` alors que le bare-name `__revit__` résoudrait
correctement. C'est un comportement spécifique au runtime CPython de
pyRevit ; sous IronPython, `__revit__` est effectivement dans
`globals()`. Ma garde défensive « propre » était paradoxalement
moins compatible que le pattern direct.

**Fix** : bare-name access avec `try/except NameError`, et fallback
explicite sur `from pyrevit import HOST_APP; uiapp = HOST_APP.uiapp`
si jamais l'injection a vraiment échoué. Le fallback documenté dans
le code commente le pourquoi pour ne pas refaire l'erreur.

**Documentation** : nouvelle ligne dans `CLAUDE.md`
("gotchas CPython") :
> `__revit__` sous CPython est en bare-name, pas dans `globals()` —
> `globals().get("__revit__")` retourne `None` même quand l'injection a
> eu lieu. Pattern correct : `uiapp = __revit__` (entouré d'un
> `try/except NameError`). Fallback : `from pyrevit import HOST_APP;
> uiapp = HOST_APP.uiapp`.

### Validation Phase 7

- `pytest -q` → **57 passed** (sanity ; le fix ne touche que le
  pushbutton runtime, qui n'est pas testé hors-Revit).
- **Validation runtime ✓** : « Refresh KG » sur un petit projet
  (1 niveau, 1 carré de 4 murs Mur 1, + le type curtain wall par
  défaut) :
  - Summary affiché : `Levels: 1 / Wall types: 2 / Walls: 4`,
    `project_id=172b17c1507be3d5`, `Persisted to: …\172b17c1507be3d5.kg.json`.
  - KG sur disque inspecté : 7 nœuds, 8 edges (`at_level` +
    `is_type` par mur), `_revit_id` stampé sur chaque nœud, action_log
    avec une entrée `rescan` + summary inline. Géométrie cohérente :
    carré ~1.4 × 1.4 m, h=4 m, p1/p2 en mètres comme attendu.
  - Le `Mur-rideau 1` (curtain wall) a remonté
    `total_thickness=0.025` sans déclencher le skip — `.Width` n'a
    pas raised malgré la nature spéciale du type. Le filet de sécurité
    per-element try/except n'a pas eu à s'activer cette fois.
  - Artifact FP mineur observé : `total_thickness=0.20000000000000004`
    au lieu de `0.2` (round-trip feet↔meters). À arrondir si la
    sérialisation gagne en lisibilité ailleurs, pas urgent.
- **Chaîne complète validée** : pyRevit → CPython → defensive shell →
  `kg_sync.open_or_create` → `full_rescan` (collectors Revit →
  convertisseurs → KG NetworkX → transaction atomique) → persistence
  JSON → TaskDialog summary. Le « morceau critique kg_sync.py »
  fonctionne bout-en-bout.

### Phase 8 — `walls_create` Revit-réel + dispatcher doc-aware

Item #2 du reste-à-faire : la réécriture des tools fakes. Bilan
scope :
- `catalog_list_*` : déjà KG-only, fonctionne tel quel. Le KG est
  populé par `full_rescan` côté Revit, par le seed CLI côté local —
  même schéma, deux sources.
- `query_find_by_name`, `aggregations_count` : KG-only, aucun
  changement.
- `walls_create` : seul tool qui *mute* le modèle, donc seul à
  réécrire pour la branche Revit.

**Param contextuel `doc` reconnu par `@tool`.** Extension symétrique
au `kg` existant : le décorateur autorise un 2e paramètre nommé
`doc` (et seulement à cette position), qu'il exclut du schema
LLM-facing. Le dispatcher `dispatch_tool_use` accepte un kwarg
`doc=None` et l'**injecte uniquement si le tool le déclare** (via
`inspect.signature`). Tools KG-only inchangés (signature
`(kg, *user_params)`), tools doc-aware = `(kg, doc, *user_params)`.

**`walls_create` doc-aware** :
- `doc is None` (CLI / pytest) → chemin KG pur, `revit_id: None` dans
  la réponse — l'absence est explicite, pas implicite.
- `doc is not None` → résout `level_eid`/`wt_eid` via
  `kg.get_revit_id` *avant* d'importer `revit_primitives`
  (sinon un test hors-Revit avec sentinel `doc=object()` partirait
  en `ModuleNotFoundError: 'Autodesk'` au lieu du `ValueError`
  attendu). Construit `XYZ`/`Line.CreateBound` en pieds, appelle
  `Wall.Create(doc, line, wt, lvl, height, 0.0, False, False)`
  (overload §Phase 1 REVIT_API_NOTES), enveloppé dans
  `rp.transaction(doc, "walls.create")` qui inclut *aussi* la
  mutation KG + le bind ElementId. Le KG outer-transaction posé par
  le dispatcher fournit la rollback symétrique en cas d'exception.

**Helper `_record_in_kg`** factorise la mutation KG (add_node + 2
edges) pour ne pas dupliquer entre les deux branches. Lisible.

### Tests Phase 8

5 tests nouveaux, tous passent en local sans Revit :

- 3 dans `test_llm_protocol.py` :
  - `test_dispatch_passes_doc_to_doc_aware_tool` — sentinel passé à
    travers, capturé dans le tool.
  - `test_dispatch_does_not_inject_doc_into_kg_only_tool` — tool
    sans `doc` reçoit pas de `doc=` kwarg même si le caller le
    passe (pas de TypeError unexpected-kwarg).
  - `test_doc_aware_tool_excludes_doc_from_schema` — `doc`
    n'apparaît pas dans `input_schema.properties`, ni `kg`.
- 2 dans `test_tools.py` :
  - `test_walls_create_revit_path_requires_revit_binding` — appel
    avec `doc=object()` sans binding ⇒ `ValueError "no Revit
    binding"`, KG inchangé.
  - `test_walls_create_kg_only_path_returns_revit_id_none` — chemin
    sans doc remonte explicitement `revit_id: None` dans la
    réponse.

### Validation Phase 8

- `pytest -q` → **62 passed en 0.86s** (57 → +5). Aucune régression
  sur les anciens tests `walls_create` (KG-only par défaut).
- Validation runtime à venir : depuis le `prompt.pushbutton` (quand
  on le câblera) ou en lançant ad hoc depuis Revit. Premier test
  réel : refresh_kg puis appel direct à `walls_create` via une
  invocation simulée pour vérifier qu'un mur Revit est bien créé
  avec son ElementId stamped dans le KG.

### Phase 9 — `prompt.pushbutton` câblé au vrai flow LLM

Dernier item Semaine 1 V0. Quatre sous-livrables, tous KG-only-testables :

**`LLMClient.run_turn` accepte `doc=None`** et le forward à
`dispatch_tool_use(..., doc=doc)`. Le dispatcher dispatche à son tour
selon que le tool déclare `doc` ou pas (Phase 8). La CLI continue à
fonctionner sans changement (signature backward-compat, `doc` en kwarg).

**Sérialisation historique JSON** (`lib/llm_api.py`) — la conversation
Anthropic mélange des dicts (user prompts, tool_results qu'on fabrique)
et des objets pydantic v2 (assistant ContentBlocks renvoyés par
l'API). `serialize_history(history)` walk chaque turn et convertit via
`.model_dump()` ; les dicts existants sont laissés en place. Defensive
wrap `{"type": "text", "text": str(...)}` pour les objets inattendus
plutôt que de planter en JSON. Pas de désérialisation symétrique
nécessaire : l'API Anthropic accepte les dicts en `messages=`
directement.

`save_history(history, path)` écrit *atomiquement* via
`tmp.replace(path)` (renommage sur même filesystem = tout-ou-rien).
`load_history(path)` retourne `[]` si le fichier n'existe pas — le
premier clic d'un projet ne fait pas d'erreur.

**`config.history_path_for(project_id)`** posé alongside
`kg_path_for`. Suffix `.history.json` (séparé du `.kg.json`). Pas
de `.context.md` Markdown comme évoqué §8 du DESIGN — V0 utilise
le format API-natif ; le Markdown human-readable peut venir plus
tard si on veut une UI de relecture.

**`prompt.pushbutton/script.py`** réécrit du sanity-check au vrai
flow, héritant des patterns durcis des Phases 4 / 6 / 7 :
- Defensive shell (BaseException + traceback).
- `__revit__` bare-name avec fallback `pyrevit.HOST_APP`.
- Save-first (PathName non vide).
- Garde supplémentaire : si KG sans `Level` ET sans `WallType`,
  refuse en pointant vers « Refresh KG ». Évite de spend des
  tokens sur un catalogue vide.

Input prompt : `Microsoft.VisualBasic.Interaction.InputBox` via
PythonNet (`clr.AddReference("Microsoft.VisualBasic")`). Single-line,
moche, *fonctionnel*. Une UI WPF multi-ligne avec markdown rendering
de la réponse pourra venir en Phase 10 ou V1.

Display : TaskDialog avec text LLM + tools utilisés + tokens / stop.

### Tests Phase 9

8 tests nouveaux, tous passent sans Revit ni clé API :

- `test_config.py` +1 : `history_path_for` résout sous `fake_home`.
- `test_llm_api.py` (nouveau fichier, 7 tests) :
  - `serialize_history` passe les strings, dumpe les blocks via
    `.model_dump`, préserve les dicts intacts, defensive-wrappe les
    objets exotiques.
  - `save_history` + `load_history` roundtrip avec mix de SDK blocks
    et tool_result dicts.
  - Empty file → `load_history` renvoie `[]`.
  - Save atomicité (pas de `.tmp` traînant après un write réussi).

Stubs pydantic via `_FakeBlock(payload)` exposant `.model_dump()` —
permet de tester sans dépendre de la version exacte du SDK Anthropic.

### Validation Phase 9

- `pytest -q` → **70 passed en 9.88s** (62 → +8). Le slowdown (de
  ~1s à ~10s) vient du `import anthropic` top-level dans `llm_api.py`,
  one-shot par run pytest, acceptable. Si gênant, déférer en lazy
  import (option YAGNI pour V0).
- Validation runtime à venir : clic « Prompt » avec un projet sauvé
  + KG rescaned, prompt type « liste les niveaux et types de mur »
  pour tester un flow read-only avant d'essayer une mutation
  (`walls_create`).

### Phase 10 — Pivot vendoring → pip-in-embedded + retrait `lib/_vendor/`

Premier clic « Prompt » dans Revit : `ImportError: No module named
'anthropic'`. Même famille que la Phase 5 (networkx), mais cette fois
la voie « vendor pure-Python sous `lib/_vendor/` » ne suffit plus :
anthropic a deux dépendances avec **extensions C natives** (`jiter`
en Rust, `pydantic_core` en Rust), inscopiables au vendoring
pure-Python qu'on avait choisi.

Inspection du dépendance-graph anthropic via la venv locale :

| Package        | Version  | Pure Python ? |
|----------------|----------|---------------|
| httpx          | 0.28.1   | ✓             |
| pydantic       | 2.13.4   | ✓             |
| jiter          | 0.14.0   | ✗ (Rust)      |
| pydantic_core  | 2.46.4   | ✗ (Rust)      |
| anyio          | 4.9.0    | ✓             |
| distro         | 1.9.0    | ✓             |
| sniffio        | 1.3.1    | ✓             |
| typing-extensions | 4.14.1| ✓             |
| docstring-parser  | (req) | ✓             |

**Décision avec l'utilisateur** : retirer entièrement le vendoring et
adopter **pip dans le CPython embarqué** comme voie unique. Raisons :
- Vendoring pure-Python n'absorbe pas les Rust/C extensions — limite
  fondamentale, pas un workaround à perfectionner.
- Pip-in-embedded est la solution standard recommandée par Python
  pour les distributions embeddable (et c'était déjà l'« alternative »
  documentée Phase 5).
- Setup per-machine acceptable une fois et pour toutes — couvre tous
  les futurs ajouts de deps (ezdxf, numpy si compliance, etc.) sans
  rejouer le débat.
- Cohérence : une seule voie d'installation, pas deux mécanismes
  parallèles à maintenir.

### Setup réalisé

1. `python312._pth` : décommenté `import site`. Ligne de commentaire
   actualisée pour pointer vers ce journal.
2. `curl -sSL https://bootstrap.pypa.io/get-pip.py -o $TEMP/get-pip.py`.
3. `python.exe get-pip.py` → pip 26.1.1 installé.
4. `python.exe -m pip install anthropic networkx` → installe 16
   wheels au total :
   `annotated-types 0.7.0 / anthropic 0.100.0 / anyio 4.13.0 /
   certifi 2026.4.22 / distro 1.9.0 / docstring_parser 0.18.0 /
   h11 0.16.0 / httpcore 1.0.9 / httpx 0.28.1 / idna 3.14 /
   jiter 0.14.0 (cp312-win_amd64) / pydantic 2.13.4 /
   pydantic_core 2.46.4 (cp312-win_amd64) / sniffio 1.3.1 /
   typing_extensions 4.15.0 / typing-inspection 0.4.2 /
   networkx 3.6.1`.
5. Sanity check :
   `python.exe -c "import anthropic, networkx, pydantic; ..."` → OK,
   `pydantic.BaseModel.model_dump` accessible (chemin utilisé par
   `serialize_history`).

### Retrait `lib/_vendor/`

- `claude-in-revit.extension/lib/_vendor/networkx/` supprimé (10.85 MB).
- `claude-in-revit.extension/lib/__init__.py` vidé (plus de bootstrap
  `_vendor` sur `sys.path` — le mécanisme tombe).
- `pyproject.toml` : retrait de `exclude = ["lib._vendor*"]` dans
  `[tool.setuptools.packages.find]` — la règle n'a plus d'objet.
- `pytest -q` après cleanup → **70 passed en 3.43s**. Pas de
  régression : le venv local résout networkx via ses propres
  site-packages, le retrait du fallback est transparent.

### Documentation actualisée

- **`CLAUDE.md`** (section « pyRevit pushbuttons — gotchas CPython ») :
  remplacé le bullet vendoring par un bullet pip-in-embedded clair,
  avec procédure de setup en 4 étapes (`_pth` → `get-pip.py` → install
  → restart). Le vendoring n'est plus mentionné comme voie viable —
  une ligne explique qu'on l'a écarté à cause des extensions C.
- **`README.md`** : retiré `lib/_vendor/` de l'arbo `lib/`,
  remplacé le paragraphe rationale par un paragraphe pip-in-embedded
  pointant vers CLAUDE.md pour la procédure détaillée.

### Validation Phase 10

- `pytest -q` → **70 passed en 3.43s**, aucune régression.
- Validation runtime à venir : reclic « Prompt » sur projet sauvé
  + KG rescaned doit maintenant passer le `import anthropic` et
  exécuter le vrai flow LLM end-to-end.

### Phase 11 — Trio de mutations walls : delete + move + set_height

Démarrage Semaine 2 V0 (§9 du DESIGN doc — géométrie complète,
UC2/UC3). `walls_create` ayant fonctionné bout-en-bout en runtime,
on étend `walls.py` avec les trois mutations universelles :

- `walls_delete(llm_id)` : `Document.Delete(ElementId)` côté Revit +
  `kg.soft_delete(llm_id)` côté KG. Soft delete par défaut conformément
  §4.1 du design doc — le nœud reste avec `deleted_at_turn=N` posé,
  exclu des queries par défaut mais préservé pour la traçabilité.
- `walls_move(llm_id, dx, dy)` : `ElementTransformUtils.MoveElement`
  côté Revit + `kg.modify_node` qui décale `p1`/`p2`. La `length`
  n'est pas touchée (translation rigide → invariante).
- `walls_set_height(llm_id, height_m)` : `wall.get_Parameter(
  BuiltInParameter.WALL_USER_HEIGHT_PARAM).Set(height_ft)` côté Revit
  + `kg.modify_node` côté KG. Vérifie le retour booléen de `param.Set`
  (peut renvoyer False si le paramètre est contraint par `Top
  Constraint` ; on lève alors un message actionnable).

**Pattern partagé** factorisé en `_require_live_wall(kg, llm_id)` :
existence, `_type == "Wall"`, non soft-deleted. Centralise les
préconditions et donne un message d'erreur uniforme pour la branche
d'erreur (« not a Wall », « already soft-deleted »).

Tous trois suivent la même architecture que `walls_create` (Phase 8) :
- `doc is None` → mutation KG uniquement (CLI / pytest, et utile pour
  des essais hors-Revit).
- `doc is not None` → résolution `revit_id` avant les imports
  Revit (sinon test hors-Revit avec sentinel partirait en
  ImportError), puis `rp.transaction(doc, name)` enveloppe l'appel
  Revit *et* la mutation KG — rollback symétrique garanti par le
  `kg.transaction()` outer posé par le dispatcher.

**Naming Anthropic** : Anthropic refuse les points dans les noms de
tools (`^[a-zA-Z0-9_-]{1,64}$`), donc `walls_delete` / `walls_move` /
`walls_set_height`. Le design doc utilise `walls.delete` en prose ;
on garde le schema underscore-only pour l'API.

### Tests Phase 11

8 tests nouveaux dans `test_tools.py` :

- 3 sur `walls_delete` : soft-delete réussit côté KG (Wall absent des
  queries par défaut, présent avec `include_deleted=True`) ; refus
  double-delete (`already soft-deleted`) ; chemin Revit unbound.
- 2 sur `walls_move` : translation p1/p2 OK côté KG (length
  inchangée) ; chemin Revit unbound (KG untouched).
- 2 sur `walls_set_height` : modif height OK côté KG ; chemin Revit
  unbound.
- 1 cross-tool : pointer un des trois verbes sur un `llm_id` qui
  n'est pas un Wall (passé un `level_001`) doit échouer avec
  "not a Wall" — exerce `_require_live_wall` sur les trois.

Fixture `kg_with_wall` qui dérive de `kg_with_seed` et ajoute un
Wall (p1=[0,0], p2=[5,0], h=2.7) via les API KG directes — évite
de devoir relancer `walls_create` pour chaque test, plus rapide et
plus prévisible.

### Validation Phase 11

- `pytest -q` → **78 passed en 2.83s** (70 → +8). Aucune régression
  sur les tests existants.
- Validation runtime à venir : trois prompts à enchaîner en Revit :
  1. « supprime le mur wall_001 » → mur disparaît dans Revit + KG
     soft-deleted.
  2. « déplace wall_002 de 2 m vers l'est » → mur translaté visuellement
     + p1/p2 KG décalés.
  3. « passe la hauteur de wall_003 à 3.5 m » → mur grandit
     visuellement + KG.height=3.5.

### Phase 12 — UX : intégration de la sélection active Revit

Question utilisateur 2026-05-11 : « et pour intégrer les
identifications de base comme la sélection active ? » — UX critique :
sans contexte de sélection, l'utilisateur doit ré-épeler le llm_id
de chaque élément (« supprime wall_003 » au lieu de « supprime ça »
après avoir cliqué le mur dans Revit).

**Approche retenue : injection dans le system prompt, pas de tool
dédié.** Le LLM voit la liste des llm_ids sélectionnés en
contexte ambient et peut les passer directement aux tools (`walls_delete`,
`walls_move`, etc.) comme refs habituelles. Un tool
`selection_get_active` serait redondant pour les cas usuels et
demanderait un round-trip API supplémentaire à chaque turn.

**Helper `kg_sync.active_selection_llm_ids(uidoc, kg)`** :
- Lit `uidoc.Selection.GetElementIds()` (renvoyant un
  `ICollection<ElementId>`).
- Résout chaque ElementId vers son llm_id via `kg.find_by_revit_id`
  (réutilise le `_extract_revit_id` qui gère le breaking change
  `Value`/`IntegerValue` 2024).
- Retourne `(llm_ids, unbound_count)`. Le compteur unbound signale
  les éléments sélectionnés non mappés dans le KG — typiquement des
  pré-existants avant un Refresh KG, ou des types qu'on ne modélise
  pas encore (doors/rooms/etc.). UX : on l'expose dans le system
  prompt pour que le LLM puisse suggérer un Refresh KG.
- Tolère `uidoc is None` et `uidoc.Selection is None` (renvoie
  `([], 0)`) pour ne pas forcer le pushbutton à pré-guarder.

**Wiring dans `prompt.pushbutton`** :
- Snapshot de la sélection *avant* d'ouvrir le formulaire de prompt
  WinForms (`ShowDialog` peut voler le focus et faire perdre la
  sélection Revit — capture préventive avant la modal).
- `_format_selection_line(ids, unbound)` construit une ligne unique
  pour le system prompt :
  - `Sélection active : aucune` si rien.
  - `Sélection active : 2 mappé(s) — wall_001, wall_003` sinon.
  - `Sélection active : 1 mappé(s) — wall_001 ; 2 non mappé(s) (lance
    Refresh KG pour les voir)` si mix.
- Bloc d'instruction ajouté au system prompt :
  > Si la requête utilise des démonstratifs (« ce mur », « ces »,
  > « this/these ») ou des pronoms implicites (« supprime-le »,
  > « déplace-les »), prends les llm_ids de la *sélection active*
  > comme cibles par défaut sans redemander de précision.

### Tests Phase 12

5 tests dans `test_kg_sync.py` (stubs `_FakeUIDoc` /
`_FakeSelection` / `_FakeElementId`) :
- Sélection vide → `([], 0)`.
- `uidoc is None` → `([], 0)` (pas de crash).
- Élément bindé → llm_id résolu correctement.
- Mix bindé/non-bindé → llm_ids des bindés + counter unbound.
- Ordre préservé : si Revit renvoie `[wt_eid, level_eid]`, l'output
  préserve `[wt_llm_id, level_llm_id]`.

### Validation Phase 12

- `pytest -q` → **83 passed en 2.84s** (78 → +5).
- Validation runtime à venir : ouvrir un projet sauvé, **cliquer un
  mur dans Revit avant de cliquer Prompt**, taper « supprime ce mur »
  → LLM doit appeler `walls_delete(llm_id=<sélection>)` sans
  redemander de précision.

### Identifications restantes à intégrer (au fil des besoins)

Sélection est l'identification UX la plus immédiate. D'autres
candidats à câbler si le besoin émerge :
- **Vue active** (`uidoc.ActiveView`) — utile pour contextualiser
  les requêtes spatiales (« sur ce plan », « dans cette coupe »).
- **Niveau actif / workplane** — pour les créations « à mon
  niveau ».
- **Phase Revit courante** — pour les workflows multi-phases.
- **Nom / chemin du projet** — pour la cohérence d'identité dans
  les rapports.

Pas urgent en V0. À traiter quand un cas concret le réclame.

### État final & reste à faire

**Acquis session 2 (sessions de l'après-midi cumulées) :**

- `lib/project_kg.py` étendu : `_revit_id` réservé, set/get/find,
  `_clear_topology` ✓
- `lib/kg_sync.py` créé : binding helpers, convertisseurs Level/WallType/Wall,
  `full_rescan` atomique + per-element skip, `@kg_synced`,
  `project_id_for`, `open_or_create` ✓
- `lib/config.py` étendu : `projects_dir`, `kg_path_for` ✓
- `lib/llm_protocol.py` étendu : `@tool` reconnaît `doc`, dispatcher
  l'injecte conditionnellement ✓
- `lib/tools/walls.py` réécrit : `walls_create` doc-aware (Revit +
  KG-only fallback), helper `_record_in_kg` ✓
- `refresh_kg.pushbutton` câblé + defensive shell + politique
  Save-first + fix `__revit__` bare-name ✓
- `lib/_vendor/networkx/` (10.85 MB) + bootstrap `lib/__init__.py` ✓
- Documentation vendoring + `__revit__` gotcha dans CLAUDE.md + README.md ✓
- `lib/llm_api.py` étendu : `run_turn(doc=...)`, `serialize_history`,
  `save_history`, `load_history` ✓
- `lib/config.py` étendu (Phase 9) : `history_path_for` ✓
- `prompt.pushbutton` câblé : open_or_create KG + load history +
  InputBox + run_turn + save history + TaskDialog summary ✓
- 70 tests verts (35 nouveaux), aucune régression ✓

**Reste à valider en runtime :**

1. **Read-only prompt** : « liste les niveaux et types de mur » →
   LLM appelle `catalog_list_levels` + `catalog_list_wall_types`,
   formate la réponse. Permet de valider le wiring sans risquer
   une mutation Revit.
2. **Mutation prompt** : « crée un mur de 5 m sur le Niveau 1
   partant de (0, 0) vers l'est, hauteur 2.7 m » → Wall apparaît
   dans Revit + KG, `revit_id` stamped, action_log loggué.
3. **Multi-turn** : 2e clic dans la foulée → history rechargée
   depuis disque, LLM se souvient des llm_ids créés.

**Dette / optionnel :**

- Arrondi FP (`round(x, 6)`) dans `kg_sync` convertisseurs pour des
  JSON nets (`0.2` au lieu de `0.20000000000000004`).
- UI WPF multi-ligne pour le prompt input + rendering Markdown de
  la réponse (`Interaction.InputBox` VB6 est fonctionnel mais
  cosmétiquement vintage).
- Trim history après N tours (§7 mentionne 3) — V0 laisse
  l'historique grossir indéfiniment.
- `LLM.md` versionné (système prompt) — actuellement inline dans
  `prompt.pushbutton`. À sortir quand on aura plus de 20 lignes
  de contenu système.

**Semaines 2-5 V0 (§9) à venir :**

- Géométrie complète : `walls_modify`, `walls_delete`, openings
  (door / window), rooms, levels, transforms (move / rotate /
  mirror / copy).
- I/O : `dwg_reader.py` + tools input.* / bulk.*.

**Reste pour la Semaine 1 V0 (mise à jour) :**

1. **Validation runtime de `refresh_kg`** : à exercer dans Revit.
   Premier KG sur disque attendu sous
   `C:\Users\lauro\.config\claude-in-revit\projects\<16hex>.kg.json`.
2. **Réécriture des tools fakes en tools Revit réels** : `walls_create`
   passe à `Wall.Create(doc, line, wallType.Id, level.Id, height, 0,
   False, False)` enveloppé dans `@kg_synced("walls.create")`. Les
   tools `catalog_list_*` deviennent un re-read du KG (plus de seed
   local). Implique de gérer la coexistence test-vs-runtime : les
   tools fakes seed le KG côté CLI, les tools réels lisent le KG
   rescané côté Revit. Une solution : injecter `doc` dans le dispatcher
   et faire des tools `doc-aware` qui no-op le seed en présence d'un
   doc Revit.
3. **Câbler le vrai flow LLM dans `prompt.pushbutton`** : prompt input
   form + persistance de l'historique entre clics (§8 :
   `<uuid>.context.md`). Inchangé.

**Dette créée :**

- Convertisseurs Element→attrs limités à 3 types (Level, WallType,
  Wall). Door/Window/Room/FamilyType à compléter en Semaines 2-3 quand
  les tools géométrie élargiront le scope.
- Murs courbés tombent sur la chord. À fixer au moment des `walls_modify`
  / `walls_move` (la modification d'un arc demande de toute façon une
  représentation différente que p1/p2).
- Persistance d'`_revit_id` à travers les sessions Revit (cf. décision
  4). À reconsidérer si on observe des collisions en production.

**Risques (§10 du design doc) :**

- Le drift `persist() fails after Revit commit` reste théoriquement
  ouvert ; mitigé par `refresh_kg`. La fenêtre est étroite (un disque
  doit raser pendant le `json.dump`), donc V0 acceptable.

---

## 2026-05-11 — Baseline versions + `lib/config.py`

### Contexte & objectif

Premier jour avec accès Revit/PyRevit, donc on lève l'incertitude de la
décision #1 d'hier (compat 3.8 retenue par défaut) et on pose la première
brique « infra » de la Semaine 1 V0 (§9 du design doc) : un loader de clé
API propre, qui décale `lib/llm_api.py` d'une dépendance à `os.environ`
vers le canal canonique `~/.config/claude-in-revit/api_key` (§8 du design
doc).

Repo fraîchement cloné sur ce poste — pas de venv hérité, à
reprovisionner. Profil Windows corporate (`HOMEDRIVE=U:` redirigé vers
`\\BAOI-FILES01`), traité en début de session côté `~/.gitconfig`
(setx HOME=C:\Users\lauro + credential.helper=manager) — sans rapport
direct avec ce repo mais nécessaire pour pousser ensuite.

### Décisions

1. **Baseline Python = 3.12** (au lieu de 3.8). Sondage local :
   - Revit 2025 (25.0.2.419), PyRevit 5.0.0.25034 (Master), engine
     DEFAULT 2712, **CPython 3.12.3** embarqué
     (`bin/cengines/CPY3123/python.exe`, `python312.dll`).
   - Venv locale construite avec Python 3.12.7 (Anaconda déjà
     présent, détecté par `uv`).
   - On lève l'auto-restriction du 2026-05-10 sur `match/case`,
     `X | Y` (PEP 604), `Self`, `list[int]`, `tomllib`, exception
     groups. `from __future__ import annotations` reste en place
     dans les fichiers existants — pas de refactor cosmétique.
   - `pyproject.toml` → `requires-python = ">=3.12"`.

2. **Précédence API key : fichier puis env var**, validée par l'intuition
   « fichier = état stable, env var = override ad hoc ». Le design doc
   §8 traite le fichier comme canonique sans figer l'ordre ; en cas de
   doute futur (CI, .env), l'env var permet l'override naturel sans
   toucher au fichier — mais quand le fichier est posé il gagne.
   *À reconsidérer* si quelqu'un veut un override env-var-first façon
   Anthropic SDK natif.

3. **`uv` comme outil de provisioning** (installé via winget,
   user-scope). Détecte automatiquement les Python existants
   (Anaconda 3.12.7 ici), zéro download. Le `pip install -e .[dev]`
   passe par `uv pip install` mais le venv reste un venv standard
   utilisable par `python -m pytest` ensuite — pas de lock-in.

### Phase 1 — Versions baseline

Fichiers modifiés :

- `pyproject.toml` : `requires-python = ">=3.8"` → `>=3.12`.
- `CLAUDE.md` :
  - Section *État du repo* mise à jour : pré-impl → slice validé
    (renvoi à l'entrée 2026-05-10).
  - Section *Stack cible* : versions Revit 2025 / PyRevit 5.0 / engine
    2712 / CPython 3.12.3 listées, paragraphe ajouté sur la baseline
    Python 3.12 et la fenêtre de features autorisées (10-12).

Pas de refactor des fichiers slice : le `from __future__ import
annotations` est inoffensif et son retrait dans une session « versions »
diluerait le diff. À traiter au cas par cas si nécessaire.

### Phase 2 — `lib/config.py`

**Objectif** : centraliser la résolution de la clé API et préparer
l'accueil de `config.json`, `context.md`, `projects/`, `extensions/`
(§8 du design doc) sans les implémenter tant que personne ne les
consomme.

Choix de design :

- **Résolveurs en fonctions** (`config_dir()`, `api_key_file()`),
  *pas* en constantes module-level. `Path.home()` est évalué à chaque
  appel — testable via `monkeypatch.setattr(Path, "home", ...)` sans
  toucher à `os.environ["HOME"]`, et robuste face à un éventuel
  re-rooting de HOME en cours d'exécution (que PyRevit ne fait pas
  aujourd'hui mais qui ne coûte rien à supporter).
- **`ConfigError(RuntimeError)`** dédiée : la consigne est de
  faire échouer la résolution avec un message actionnable
  (chemin attendu + nom de la variable d'env), pas de laisser
  `anthropic.AuthenticationError` partir en mi-tour.
- **Fichier d'abord, env var ensuite** (décision 2 ci-dessus). Fichier
  vide → erreur explicite (pas de fallback silencieux env var,
  considéré comme bug de config).
- **`.strip()`** systématique des deux côtés — un fichier collé depuis
  le portal contient souvent une newline finale, un env var collé
  depuis un copier-coller peut avoir des espaces accidentels.

### Phase 3 — Intégration dans `LLMClient`

- `lib/llm_api.py` : `LLMClient.__init__` reçoit un `api_key:
  Optional[str] = None` ; si None, appel à `config.get_api_key()`.
  La clé est passée explicitement à `anthropic.Anthropic(api_key=...)`
  — préféré au pattern « set `os.environ` puis appeler `Anthropic()` »
  qui dépose une variable d'env globale spookey.
- `scripts/cli.py` : docstring mise à jour pour pointer vers les deux
  canaux de résolution (fichier + env var). Aucun changement de
  comportement à l'appel : `LLMClient()` continue de fonctionner.

### Phase 4 — Tests

`tests/test_config.py` (8 tests) :

- Cas heureux : fichier présent → clé renvoyée ; whitespace strippé.
- Fallback : fichier absent, env var posée → clé renvoyée.
- Précédence : fichier + env tous deux posés → fichier gagne.
- Erreurs : fichier vide (uniquement whitespace), tout absent, env var
  vide / whitespace-only.
- Sanity : `config_dir()` / `api_key_file()` pointent bien sous le
  `fake_home` du fixture.

Fixture `fake_home` autouse : `monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))`
puis `monkeypatch.delenv(ENV_VAR_NAME, raising=False)` pour partir
d'un état neutre. `_write_key()` helper crée la sous-arborescence
`~/.config/claude-in-revit/` dans le tmp et y écrit le contenu
demandé.

### Validation

- Venv reprovisionnée from scratch : `uv venv --python 3.12` →
  Python 3.12.7 ; `uv pip install -e ".[dev]"` (anthropic, networkx,
  pytest, pytest-mock + transitifs).
- `pytest -v` → **35 passed en 3.32s** (27 anciens + 8 nouveaux,
  aucun warning, aucun test cassé).
- Pas de test live API ce coup-ci : `config.get_api_key()` est
  trivial mécaniquement et le flow Anthropic a été validé hier.
  À refaire dès qu'un changement touche `LLMClient.run_turn` ou le
  payload tools.

### Phase 5 — Bootstrap extension PyRevit (restructure)

**Objectif** : passer du layout slice plat à la structure cible §3 du
design doc, sans casser le harnais hors-Revit.

Inventaire local d'abord, pour lever toute hypothèse :

- Revit 2025 (25.0.2.419), pyRevit 5.0.0.25034 (Master), engine
  DEFAULT 2712 (= IronPython 2.7.12), CPython 3.12.3 embarqué.
- Le default engine est **IronPython 2.7** — il faut donc explicitement
  flagger CPython sur nos scripts. Le canal officiel (lu dans
  `extensions/pyRevitCore.extension/.../Settings.smartbutton/SettingsWindow.xaml`)
  est le directive **`#! python3`** en première ligne du `script.py`.
  Pas via `bundle.yaml` (qui n'accepte que `engine.persistent` et
  `engine.full_frame`), ni via suffixe de dossier.

Restructure faite :

- `lib/` (à la racine) → `claude-in-revit.extension/lib/` via
  `Move-Item`. Pas de `git mv` parce que `config.py` et
  `tests/test_config.py` ne sont pas encore tracked ; git fera le
  rename detection au commit via similarity index.
- `claude-in-revit.extension/extension.json` : metadata standard
  (`name`, `description`, `author`, `url`, `type: extension`,
  `author_profile`). Calqué sur l'`extension.json` du `pyRevitCore`.
- `claude-in-revit.extension/LLM.tab/agent.panel/`
  (renommé en `LLM.tab` en fin de session pour un libellé d'onglet
  court dans la ribbon Revit ; auparavant `claude-in-revit.tab`)
  créé avec 3 `*.pushbutton/` (prompt, globals, refresh_kg). Chaque
  pushbutton a un `bundle.yaml` (`title`, `tooltip`, `author`) et un
  `script.py` shebang-flagué CPython.
- `pyproject.toml` : `[tool.setuptools.packages.find].where =
  ["claude-in-revit.extension"]` et
  `[tool.pytest.ini_options].pythonpath = ["claude-in-revit.extension"]`
  pour que le venv local trouve toujours `lib` comme package à la
  même profondeur.
- `scripts/cli.py` : le path-fixup pointe maintenant sur
  `<repo>/claude-in-revit.extension/` (au lieu du repo root), pour
  miroir exact du runtime pyRevit.

`uv pip install -e ".[dev]"` rerun pour rafraîchir l'editable install
après le move ; pytest → **35 passed** en 0.75s, aucun fichier touché
dans `lib/` ou `tests/`.

Enregistrement avec pyRevit :

- `pyrevit extensions paths add C:\Users\lauro\Documents\IT\claude-in-revit`
  (ajoute le repo comme search path, le scan trouvera l'extension
  child). Réversible via `paths forget`.
- `pyrevit env` confirme :
  `claude-in-revit | Type: UIExtension | Repo: "" | Installed: "C:\Users\lauro\Documents\IT\claude-in-revit\claude-in-revit.extension"`.
- `pyrevit extensions search claude-in-revit` retourne le match.

### Phase 6 — Découverte : pyRevit met `lib/` sur sys.path, pas la racine

Avant de promettre que `from lib import config` marche dans le
pushbutton, plongée dans le source pyRevit. Deux fichiers décisifs :

- `pyrevitlib/pyrevit/extensions/genericcomps.py:158-164` :
  pour chaque composant (extension, tab, panel, pushbutton), si
  `<comp>/lib/` existe, c'est *ce dossier-là* (pas le parent) qui est
  ajouté à `module_paths`. Et `module_paths` est propagé vers tous les
  sous-composants (lignes 445-462 — `add_module_path` itère sur les
  enfants).
- `pyrevitlib/pyrevit/loader/sessionmgr.py:562-588` :
  les `sys_paths` du script runtime sont construits à partir de ces
  `module_paths`. Pour un pushbutton, sys.path reçoit
  `<extension>/lib/`, le `bin/` du composant si présent, et le
  répertoire du pushbutton lui-même — **pas la racine de l'extension**.

**Conséquence** :

- `import config` (top-level) : OK (config.py est à la racine du
  sys.path entry `<extension>/lib/`).
- `from lib import config` : **KO** dans pyRevit (lib n'est pas un
  package atteignable depuis sys.path).
- `from .config import ...` (intra-`lib/`) : **KO** aussi (pas de
  contexte de package pour les top-level modules de `lib/`).

Notre code slice utilise *partout* `from .module import ...` et `from
lib import ...` (tests, cli, et imports internes dans `llm_api.py`).
Deux options évaluées :

- **Path A** : refactor lib/ vers bare imports (10 fichiers touchés —
  llm_api, llm_protocol, project_kg, tools/__init__, tools/*.py, tests,
  cli, pyproject). Tests et pyRevit utilisent alors le même style.
- **Path B** : fixup `sys.path.insert(0, <extension>)` au top du
  `script.py` du pushbutton. 3 lignes par pushbutton, lib intact, tests
  intacts. *Inconvénient* : nos tests locaux ne reflètent plus
  exactement l'import-path pyRevit ; risque de petits bugs d'import qui
  ne se voient qu'à l'exécution dans Revit.

**Décision** : Path B pour ce soir, validé avec l'utilisateur. Path A
restera ouvert comme dette à régler quand l'inconvénient devient
réel. Le fixup est placé dans `prompt.pushbutton/script.py` (le seul
qui importe de `lib/` aujourd'hui) ; `globals.pushbutton` et
`refresh_kg.pushbutton` n'en ont pas besoin tant qu'ils sont stubs
(YAGNI). Quand ils consommeront `lib/`, on rajoutera le fixup.

### Validation Phase 5-6

- `pytest -q` après le move + reinstall editable → **35 passed** en
  0.75s. Aucune régression.
- `pyrevit extensions search claude-in-revit` → 1 match. `pyrevit env`
  liste l'extension sous "Installed Extensions" avec type UIExtension
  et chemin correct.

### Phase 7 — Premier test runtime + bug `pyrevit.forms`

Premier clic sur "Prompt" dans Revit 2025. L'extension est bien chargée
(l'onglet et le bouton apparaissent, l'engine CPython est bien
sélectionné — le traceback montre `PyRevitLabs.PyRevit.Runtime.CPythonEngine.Execute`).
Le script tourne, mais plante immédiatement :

```
"pyrevit.forms" is not currently supported under CPython
  File "...\pyrevit\forms\__init__.py", line 27
  raise PyRevitCPythonNotSupported('pyrevit.forms')
```

**Cause racine** : `pyrevit.forms` est une couche IronPython qui utilise
des composants .NET/WPF non portés sur CPython/PythonNet. C'est une
limitation connue de pyRevit (pas un bug de notre code). Le bug est
côté hypothèse — j'ai posé `from pyrevit import forms` par habitude
slice CLI sans vérifier la compat CPython.

**Fix** : tous les pushbuttons passent à `Autodesk.Revit.UI.TaskDialog`,
qui est l'API Revit native, toujours accessible via PythonNet dans
pyRevit. Plus de dépendance sur `pyrevit.forms`. À l'usage la signature
est différente (`TaskDialog.Show(title, message)` au lieu de
`forms.alert(message, title=)`), donc petit lift mais pas grave.

**Côté positif du crash** : il confirme TROIS choses importantes du
bootstrap, avant même que `lib.config` ne soit testé :

1. L'extension est bien chargée par pyRevit (la ribbon a poussé).
2. Le directive `#! python3` est honoré — c'est bien le CPython engine
   qui exécute (sinon l'erreur aurait été un SyntaxError IronPython
   sur nos f-strings ou type hints).
3. Le `script.py` est trouvé et son contenu exécuté jusqu'à la ligne
   25 (l'import qui a planté).

Donc la mécanique « extension + CPython + chargement de script » est
*validée par ce crash*. Le path-fixup `sys.path` et `lib.config` n'ont
pas encore été exercés, mais c'était l'étape *après* l'import qui a
échoué — la prochaine tentative ira plus loin.

**Documentation** : trois conventions ajoutées dans `CLAUDE.md`
("pyRevit pushbuttons — gotchas CPython") pour ne pas refaire la même
erreur : `pyrevit.forms` interdit, path fixup obligatoire pour
`lib.*`, shebang `#! python3` obligatoire en ligne 1.

### Validation Phase 7

Reclic "Prompt" après la mise à jour des trois `script.py` →
**TaskDialog s'affiche correctement**, confirmation utilisateur en fin
de session (*« effectivement c'est passé »*). La chaîne complète est
donc validée bout-en-bout :

- Extension chargée par pyRevit ✓
- Engine CPython 3.12.3 sélectionné via `#! python3` ✓
- Path fixup `sys.path` opérationnel, `from lib import config`
  résout correctement depuis le pushbutton ✓
- `lib.config.get_api_key()` lit bien
  `C:\Users\lauro\.config\claude-in-revit\api_key` depuis le runtime
  pyRevit ✓
- `Autodesk.Revit.UI.TaskDialog` accessible sans `clr.AddReference`
  préalable (pyRevit pre-load des assemblies Revit dans le CPython
  embarqué) ✓

Foulée renaming en clôture : l'onglet **`claude-in-revit.tab`** →
**`LLM.tab`** (libellé court dans la ribbon). Pas d'autre impact que
le changement de nom de dossier + propagation dans `CLAUDE.md`,
`README.md`, `DESIGN.md`.

**Gotcha empirique** : un Reload depuis la ribbon pyRevit ne suffit pas
après un rename de tab — au prochain clic d'un bouton de la nouvelle
tab, Revit balance *« Échec de la commande externe — This property
must be set before runtime is initialized »*. Cause racine probable :
le cache d'assembly pyRevit (`%APPDATA%\pyRevit\Master\Logs\` + DLLs
générées par session) garde encore une référence à l'ancien nom de
tab, et le re-link au moment du clic échoue. **Fix : redémarrer Revit
complètement**. Confirmé en session — après restart, tous les boutons
de l'onglet LLM fonctionnent. À retenir : tout rename de dossier
`.tab/.panel/.pushbutton/.extension` doit s'accompagner d'un Revit
restart (et idéalement d'un `pyrevit caches clear 2025` si Revit
était ouvert pendant le rename).

**Si futur crash** :

- `ImportError` sur `Autodesk.Revit.UI` → pas vu en pratique
  (l'assembly est pre-loaded par pyRevit), mais si ça arrivait il
  faudrait un `import clr; clr.AddReference("RevitAPIUI")` avant.
- `ImportError` sur `lib.config` → le path fixup est mal calé ;
  inspecter `__file__` dans le script et recompter les niveaux de
  `os.path.dirname`.
- `ConfigError` → vérifier `~/.config/claude-in-revit/api_key`
  (mais ça a été validé via `get_api_key()` dans le CLI plus tôt
  dans la session, donc improbable).

### Phase 8 — `lib/revit_primitives.py` (foundation)

**Objectif** : poser la première brique Revit-API du V0 Semaine 1, sur
laquelle s'appuieront `kg_sync.py` et la réécriture des tools fakes
slice (`walls_create`, `catalog_list_*`) en tools Revit réels. Scope
volontairement restreint pour éviter d'engloutir une session.

Contenu :

- **`transaction(doc, name)`** — context manager qui ouvre une
  `Autodesk.Revit.DB.Transaction`, commit sur succès, rollback sur
  exception (avec re-raise pour préserver la stack trace). Garde-fous
  `HasStarted()` / `HasEnded()` contre les double-end states.
- **Conversions d'unités** — `meters_to_internal()`,
  `internal_to_meters()`, et les équivalents `sqm_*` pour les
  surfaces. Utilisent **`UnitTypeId.Meters` / `SquareMeters`** (API
  post-2022, ForgeTypeId style). Le vieux `DisplayUnitType` est
  délibérément évité — déprécié depuis Revit 2022, à fortiori cassé
  dans certains contextes 2024+.
- **Collectors** — `collect_by_category()`,
  `collect_types_by_category()` génériques + raccourcis `walls()`,
  `wall_types()`, `levels()`. `list(...)` autour des collectors parce
  que les `FilteredElementCollector` Revit sont des itérateurs one-shot
  (re-itérer renvoie silencieusement zéro résultat — piège classique).
- **`levels()` utilise `OfClass(Level)` et non `OfCategory(OST_Levels)`** :
  le filter par catégorie n'a pas un comportement stable pour les
  Levels en 2024+, alors que `OfClass` est documenté comme la voie
  fiable.

Choix de design :

- **Module Revit-only** : imports `Autodesk.Revit.DB.*` au top-level,
  sans try/except ni stubbing. Conséquence : le module n'est pas
  importable depuis le venv local (pas de PythonNet, pas de Revit
  assemblies). Pour les tests, on **n'importera pas ce module** — il
  n'apparaîtra que dans les call paths déclenchés depuis un pushbutton.
- **Pas de `clr.AddReference` explicite** : pyRevit pre-load les
  assemblies Revit dans le CPython embarqué avant d'exécuter le script,
  donc les `from Autodesk.Revit.DB import ...` résolvent
  directement. Si on devait un jour exécuter du code hors pushbutton
  (e.g. unit test sous Revit), il faudrait ajouter `clr.AddReference("RevitAPI")`.
- **Retours bruts** : les collectors retournent des `Element` Revit,
  pas des dicts. La conversion vers le schéma KG sera dans
  `kg_sync.py` — séparation de responsabilités, on évite que ce
  module connaisse le schéma du graphe.

### Validation Phase 8

- `pytest -q` → **35 passed** en 0.76s. Le nouveau module n'est
  importé par aucun test (donc aucune `ImportError` due aux imports
  Autodesk.Revit.DB hors-Revit) — confirmé par la pass clean.
- Validation runtime à venir : sera exercée à la première utilisation
  réelle dans `kg_sync.py` ou dans la réécriture de `walls_create`.

### État final & reste à faire

**Acquis cette session :**

- Baseline Python 3.12 verrouillée et documentée ✓
- `lib/config.py` : `get_api_key()`, `config_dir()`, `api_key_file()`,
  `ConfigError` ✓
- `LLMClient` autonome (plus de couplage implicite à `os.environ`) ✓
- Venv reprovisionnée via `uv` (workflow `uv venv` + `uv pip install
  -e .[dev]`) ✓
- Structure cible extension PyRevit en place
  (`claude-in-revit.extension/lib/` + `LLM.tab/agent.panel/` +
  3 pushbuttons) ✓
- Repo enregistré comme search path pyRevit ; sanity check « Prompt »
  validée bout-en-bout dans Revit 2025 ✓
- `lib/revit_primitives.py` : `transaction()` context manager, unit
  conversions m↔feet & m²↔sqft, collectors walls/wall_types/levels ✓
- 35 tests verts (avant le move, après le move, et après
  revit_primitives.py) ✓

**Dette créée :**

- Path B pour l'import-path : fixup `sys.path` dans
  `prompt.pushbutton/script.py`. À garder en tête si on ajoute un
  nouveau pushbutton qui consomme `lib/` — il lui faut le même fixup.
  Plus propre à terme : refactor `lib/` vers des bare imports
  (Path A), mais reporté.

**Reste pour la Semaine 1 V0 (§9 du design doc), dans l'ordre :**

1. **`kg_sync.py` + décorateur `@kg_synced`** : full re-scan Revit →
   reconstruction du KG projet, conversion `Element` → noeud KG
   (`revit_primitives.py` retourne du brut, ce module fait le mapping
   vers le schéma de `project_kg.py`). Le décorateur compose
   `revit_primitives.transaction(doc, name)` et
   `kg.transaction()` pour assurer l'atomicité §4.1 — rollback
   symétrique des deux côtés si l'un des deux pète.
2. **Réécriture des tools fakes en tools Revit réels** : `walls_create`,
   `catalog_list_levels`, `catalog_list_wall_types`, etc. — en utilisant
   `revit_primitives.*` + `@kg_synced`. Les tools slice restent
   utilisables pour les tests hors-Revit ; à voir si on les garde
   en parallèle ou si on injecte un fake-doc dans les tests.
3. **Câbler le vrai flow LLM dans `prompt.pushbutton`** : prompt input
   form (option simple : `Microsoft.VisualBasic.Interaction.InputBox`
   via PythonNet — moche mais zéro dépendance ; option propre : WPF
   custom). Persistance de l'historique de conversation entre clics
   (CPython3 chaque clic = process neuf, donc disque obligatoire —
   fichier dans `~/.config/claude-in-revit/projects/<uuid>.context.md`
   §8).
4. **Choix de l'identifiant projet** : §8 du design doc liste deux
   options (paramètre partagé Revit `claude-in-revit.project_uuid`,
   ou hash de `Document.PathName`). À trancher au moment de wirer la
   persistance du KG dans le pushbutton.
5. Petite dette laissée par 2026-05-10 : `ProjectKG.load(path)` ne
   peuple pas `persist_path`. À fixer quand on touchera le module
   pour les besoins de `kg_sync.py`.

**Risques (§10 du design doc) :**

- Le rollback symétrique Revit ↔ KG dans `@kg_synced` est *le* point
  délicat de l'atomicité §4.1. Hier on a posé `kg.transaction()`
  (snapshot deepcopy + restauration sur exception) ; côté Revit on
  utilise les `Transaction` natives. La séquence « rollback Revit
  d'abord, puis KG » vs. « KG d'abord, puis Revit » a des conséquences
  différentes si la 2ᵉ rollback échoue — à documenter explicitement
  dans le décorateur.

---

## 2026-05-10 — Slice vertical V0 : KG + dispatcher + LLM end-to-end

### Contexte & objectif

Repo en pré-implémentation : seul `DESIGN.md` (~940 lignes,
spec verrouillée §12) et un venv Python 3.13 existaient. Pas d'accès Revit
avant demain. Décision validée par l'utilisateur : **construire un slice
vertical bout-en-bout avec Revit stubbé**, plutôt que de bétonner le schéma
KG en isolation. L'idée : exercer en parallèle l'API Anthropic (caching,
multi-turn tool use), le registry de tools, le KG, et le dispatcher. Donne
un banc de test réutilisable pour les primitives compliance qui n'ont pas
besoin de Revit.

Cible : un `python -m scripts.cli` qui prend un prompt, appelle Sonnet 4.6
avec un catalogue de tools tier-1, dispatche les tool calls vers des
fonctions qui mutent un KG NetworkX persisté en JSON, et boucle jusqu'à
`stop_reason=end_turn`.

### Décisions d'amorçage

Validées avec l'utilisateur via deux questions ciblées en début de session :

1. **Compatibilité Python 3.8** par sécurité. Le runtime de prod est CPython3
   embarqué dans PyRevit, version inconnue jusqu'à demain. La venv locale
   est en 3.13 mais le code dans `lib/` doit tourner identiquement sous
   PyRevit. Conséquence : `from __future__ import annotations` partout,
   pas de `match/case`, pas de `X | Y` (PEP 604), pas de `Self`, pas de
   `list[int]` direct (utiliser `typing.List[int]`).
2. **Clé API via env var `ANTHROPIC_API_KEY`** pour le slice. Le câblage
   `~/.config/claude-in-revit/api_key` (§8 du design doc) viendra avec
   `config.py` en Semaine 1 du V0.

Choix techniques implicites :

- **Boucle multi-turn manuelle** plutôt que `client.beta.messages.tool_runner()`.
  Le dispatcher mute le KG dans une `kg.transaction()` atomique entre
  chaque tool call ; on veut la visibilité totale, pas un blackbox.
- **Layout repo flat** : pas de `claude-in-revit.extension/` tant que pas
  d'accès Revit. `lib/` à la racine, importable à la fois par PyRevit
  (qui auto-injecte `lib/` dans `sys.path`) et par le harnais CLI local.
- **Modèle par défaut Sonnet 4.6** (cohérent avec §3 du design doc).
  Adaptive thinking *off* par défaut dans le slice (coûteux, peu utile
  pour valider la mécanique). `effort=medium`.

### Phase 1 — Scaffold du repo

**Objectif** : structure importable en local et compatible avec la future
extension PyRevit.

Fichiers créés :

- `pyproject.toml` — packaging editable pour `pip install -e .[dev]`.
  Deps : `anthropic>=0.40.0`, `networkx>=3.0`. Dev : `pytest>=8.0`,
  `pytest-mock>=3.12`. `requires-python = ">=3.8"`. `tool.pytest.ini_options`
  avec `pythonpath = ["."]` pour que les tests trouvent `lib/`.
- `.gitignore` — Python standards + `.venv/` + `scratch_kg/` (les KG
  dumps locaux du CLI).
- `lib/__init__.py`, `lib/tools/__init__.py`, `scripts/__init__.py`,
  `tests/__init__.py` — packages.
- `lib/tools/__init__.py` — auto-importeur : itère sur les `*.py` du
  dossier (hors `_*`) et `import_module(...)` chacun. Ainsi tout fichier
  ajouté dans `lib/tools/` se déclare automatiquement au registry via
  ses décorateurs `@tool`.

Pas de README à ce stade (règle : pas de docs sauf demande explicite),
donc `readme = "README.md"` retiré du pyproject pour éviter l'erreur de
build setuptools.

### Phase 2 — `lib/project_kg.py`

**Objectif** : représentation graphique typée du modèle Revit, persistée
en JSON, avec lifecycle action-grained et transactions atomiques.
Implémentation V0 du §4.1 du design doc, sans le binding Revit
(`kg_sync.py`, `@kg_synced`) qui viendra avec l'accès Revit.

Choix de design :

- **`nx.MultiDiGraph`** — multi parce qu'on veut potentiellement plusieurs
  arêtes entre une même paire (ex : un Wall → un Level via `at_level` *et*
  → un autre Wall via `connects_at`). Le `key` de l'arête multi est
  l'edge type lui-même (`at_level`, `is_type`, …) → unicité par type
  entre paire.
- **Schéma déclaré en haut du fichier** (`NODE_TYPES`, `EDGE_TYPES`)
  avec required/optional sets. Validation à `add_node` : refus des
  attributs inconnus (évite que le LLM injecte du bruit dans le KG).
- **llm_id auto-généré** par compteur typé (`level_001`, `wall_003`).
  Format aligné avec les exemples du design doc (`room_07` etc.).
  Possibilité de passer un llm_id explicite (utile pour l'import Revit
  futur).
- **Lifecycle attrs** centralisés en constantes (`CREATED_AT`,
  `MODIFIED_AT`, `DELETED_AT`). Soft delete par défaut conformément
  §4.1.
- **Action log** : list de dicts `{turn, action, target, details}`,
  granularité par action (pas par tour) — décision verrouillée §12.
- **Transaction atomique** : context manager qui prend un snapshot
  via `copy.deepcopy(self.to_dict())` à l'entrée, restaure l'état sur
  exception, persiste à la sortie réussie. Le snapshot via
  `to_dict`/`from_dict` est volontaire — c'est le format de
  persistence donc on teste la roundtrip à chaque transaction. Coût
  acceptable pour les tailles cibles (NetworkX scale jusqu'à 100K
  nœuds, nos projets restent loin sous ce seuil — risque §10).

Méthodes exposées : `add_node`, `modify_node`, `soft_delete`,
`add_edge`, `has_node`, `get_node`, `find_by_type`, `find_by_name`,
`count_by_type`, `diff_since(turn)` (pour le KG diff context §6),
`to_dict`/`from_dict`, `persist`/`load`, `transaction()`.

### Phase 3 — `lib/llm_protocol.py`

**Objectif** : registry de tools auto-peuplé par décorateurs, parsing
docstring conventionnelle, génération du JSON schema attendu par
l'API Anthropic, dispatcher qui exécute un `tool_use` dans une
transaction KG.

Choix de design :

- **Décorateur `@tool(name=..., tier=...)`** : la fonction reçoit `kg`
  comme premier paramètre nommé (convention obligatoire, vérifiée à
  l'enregistrement). Le décorateur exclut `kg` du schéma LLM-facing —
  c'est un paramètre de contexte injecté par le dispatcher. Le reste
  des paramètres devient le `input_schema`.
- **Parsing docstring** : sections `Concepts:` (csv), `Phrases:`
  (chaînes entre guillemets), `Similar:` (csv), `Args:` (une ligne
  `name: description` par paramètre), `Returns:`. Description = tout
  avant le premier header. Convention obligatoire §4.2 du design doc.
- **Type hints → JSON schema** : `str` → `string`, `int` → `integer`,
  `float` → `number`, `bool` → `boolean`, `dict`/`Dict` → `object`,
  `list`/`List[X]` → `array` avec `items` récursif, `Optional[X]` →
  schéma de X mais hors `required`.
- **Tool names sans points** : règle Anthropic, regex
  `^[a-zA-Z0-9_-]{1,64}$`. Convention adoptée :
  `<file>_<function>` → `walls_create`, `query_find_by_name`,
  `catalog_list_levels`. Le design doc utilise des points en prose
  (`walls.create`) ; on garde ça en doc, mais l'API utilise les
  underscores.
- **Dispatcher** : enveloppe l'appel dans `kg.transaction()`, stringify
  le résultat en JSON pour le `tool_result.content`, retourne un dict
  `{type: tool_result, tool_use_id, content, is_error}` prêt à append.
  Sur exception : KG roll-back, `is_error: True`, traceback tronqué
  (3 niveaux) inclus pour que le LLM puisse adapter.
- **`tier_max`** sur `tools_as_anthropic_payload` — préparation de la
  sélection tier-1/tier-2 (§5 du design doc). Pour le slice, tous les
  tools sont tier-1.

### Phase 4 — `lib/llm_api.py`

**Objectif** : wrapper `anthropic.Anthropic` avec boucle multi-turn
manuelle, prompt caching, accumulation des token usage.

Choix de design :

- **Boucle manuelle** (et non `tool_runner`) — voir Décisions
  d'amorçage. La boucle :
  1. Append user prompt à `history`
  2. Tant que `max_iterations` non atteint : `messages.create`,
     accumule usage, append assistant turn, si `stop_reason == "tool_use"`
     dispatche chaque `tool_use` block en parallèle (séquentiel pour
     le moment, parallélisable plus tard), append `user` turn avec
     les `tool_result`, continue ; sinon break.
- **Cache breakpoint sur le system prompt** : le system est passé
  comme liste de blocs `[{type:text, text:..., cache_control:{type:ephemeral}}]`.
  Sous le seuil du modèle (Sonnet 4.6 = 2048 tokens) le breakpoint
  no-op silencieusement ; au-dessus, on récupère ~90 % de réduction
  sur le préfixe rendu (`tools` + `system` ensemble). Aligné §7 du
  design doc.
- **Adaptive thinking** : *off* par défaut. Activable via
  `thinking="adaptive"`. Skip auto sur la famille Haiku (pas de
  thinking supporté).
- **Effort parameter** : `medium` par défaut, dans `output_config`
  (pas top-level — c'est un piège connu de l'API). Skip auto sur
  `claude-haiku-4-5` et `claude-sonnet-4-5` (renvoient 400 sinon).
- **`TurnUsage` dataclass** : accumule `input_tokens`,
  `output_tokens`, `cache_creation_input_tokens`,
  `cache_read_input_tokens`, `api_calls` à travers tous les
  round-trips d'un même tour utilisateur. Permet de tracer le coût
  par tour, et de vérifier le cache hit rate (devrait monter à
  partir du 2ᵉ tour).
- **`TurnResult`** : retourne `text`, `stop_reason`, `tool_calls`
  (liste pour debug), `usage`. Le caller (CLI) décide quoi afficher.

### Phase 5 — Tools fakes

**Objectif** : 5 tools tier-1 qui exercent le slice end-to-end :
read-only catalogue + write avec dépendances + lookup + agrégation.

Fichiers créés dans `lib/tools/` :

- `catalog.py` — `catalog_list_levels`, `catalog_list_wall_types`.
  Read-only, listent les nœuds typés du KG (en V1 réelle ces tools
  iront chercher dans Revit via `Document.GetElements`).
- `walls.py` — `walls_create(level_ref, wall_type_ref, p1, p2, height)`.
  Calcule la longueur depuis p1/p2 (vérifie que la transaction marche
  via `add_node` + `add_edge` × 2), valide les refs avant écriture.
- `query.py` — `query_find_by_name(name, node_type=None)`. Optional
  type filter, exercise le décorateur sur un paramètre `Optional[str]`.
- `aggregations.py` — `aggregations_count(node_type)`. Comptage simple,
  utilisé par les tests pour vérifier la dispatch.

Tous les tools suivent la convention docstring stricte (Concepts /
Phrases / Similar / Args / Returns) — c'est la base de l'introspection
qui alimentera le KG logiciel V1+ (§4.2).

Bootstrap implicite : `walls_create` requiert un `level_ref` et un
`wall_type_ref` existants. Le CLI seede automatiquement 2 Levels
(N00, N01) et 1 WallType (STD200) à la création d'un nouveau KG.
Réaliste comme flow : avec Revit, ces nœuds seraient déjà présents.

### Phase 6 — `scripts/cli.py`

**Objectif** : harnais REPL pour valider le slice à la main.

Comportement :

- `--persist-path` (défaut `scratch_kg/slice-demo.kg.json`) — KG chargé
  s'il existe, créé+seedé sinon.
- Bug subtil rencontré : `ProjectKG.load(path)` retourne un KG sans
  `persist_path` peuplé. Fix : le CLI le set explicitement après
  `load()`. Ça mériterait d'être corrigé dans `load` directement —
  noté pour plus tard.
- Affiche après chaque tour : tokens (in / out / cache write / cache
  read / api calls), liste des tools utilisés, `stop_reason`. Permet
  de voir la cache hit rate monter à partir du 2ᵉ tour.
- Commandes spéciales : `:kg` affiche taille du graphe, `quit`/`exit`/
  `:q` sort proprement, Ctrl-D aussi.
- Path-fixup en haut du fichier (`sys.path.insert(0, repo_root)`) pour
  que `python scripts/cli.py` marche sans `pip install -e .` — au
  prix de 3 lignes laides mais pragmatiques.

### Phase 7 — Tests + bug fixes

**Objectif** : valider la mécanique avant le premier vrai appel API.

Fichiers créés :

- `tests/test_project_kg.py` — 11 tests : ajout de nœuds + lifecycle
  attrs, refus du type inconnu / attrs manquants / attrs unknown,
  modification + log, soft delete + filtrage, validation arêtes,
  roundtrip persistence, transaction commit & rollback, `diff_since`.
- `tests/test_llm_protocol.py` — 10 tests : parsing docstring (toutes
  sections, sections manquantes, doc vide), enregistrement + schema
  généré, refus de fonction sans `kg`, filtrage tier, dispatcher
  succès / unknown tool / rollback sur exception / sérialisation
  non-ASCII. Fixture `_clean_registry` autouse qui appelle
  `reset_registry()` avant et après chaque test.
- `tests/test_tools.py` — 6 tests : registry canonique contient les
  5 tools attendus, chaque tool peut être dispatché, `walls_create`
  pose les arêtes `at_level` + `is_type`, refus avec `level_ref`
  inconnu (KG inchangé), `query_find_by_name`, `aggregations_count`.

**Trois bugs à la première run de pytest, tous instructifs :**

1. **Regex docstring trop strict** — `_SECTION_HEADER_RE = r"^(Concepts|...):\s*$"`.
   Le `$` exige que la ligne se termine après le `:`, mais la
   convention du design doc est `Concepts: ouverture, fenêtre, ...`
   (contenu sur la même ligne que le header). **Cause racine** :
   confusion entre header en deux lignes (Args:) et header inline
   (Concepts:). **Fix** : retirer le `$`, le `\s*` consomme l'espace
   ou le retour-ligne après le `:`.

2. **`from __future__ import annotations` casse `inspect.signature`** —
   Avec ce future, toutes les annotations deviennent des strings au
   runtime. `param.annotation == "int"` (string) au lieu de `int`
   (type). Le fallback `_annotation_to_schema(string, ...)` partait
   en `{"type": "string"}` pour tous les paramètres, peu importe le
   vrai type. **Cause racine** : ne pas avoir distingué les annotations
   *runtime* des annotations *string-defer*. **Fix** : remplacer
   `param.annotation` par `typing.get_type_hints(fn).get(param.name, str)`,
   qui évalue les strings dans `fn.__globals__`. Robuste si l'éval
   échoue (try/except → dict vide → fallback `str`).

3. **`reset_registry()` ne suffisait pas pour rejouer l'auto-import** —
   Premier essai : `_REGISTRY.clear()`. Mais `lib.tools` reste dans
   `sys.modules`, donc `from . import tools` au prochain
   `get_registry()` no-op silencieusement, registry reste vide,
   tests `test_tools.py` cascadent en `Unknown tool: ...`.
   Deuxième essai : ajouter `del sys.modules['lib.tools.*']`. Toujours
   pas suffisant : Python attache aussi les sous-modules comme
   *attributs* sur le package parent (`lib.tools` reste accessible
   via `lib.tools` même après `del sys.modules['lib.tools']`).
   **Cause racine** : double mécanisme de cache dans le système
   d'import Python (`sys.modules` + attributs sur le parent).
   **Fix** : dans `reset_registry`, purger les deux —
   `del sys.modules[...]` *et* `delattr(parent, "tools")`.

Après ces trois fixes : **27/27 tests passent**.

### Validation

- `pytest -v` → 27 passed en 0.25s. Couvre tout sauf le vrai appel API
  (à faire en live demain ou sur demande utilisateur, coût réel).
- Layout final vérifié via `find` — pas de `.pyc` qui traîne, pas de
  fichier orphelin.

### Validation live (Anthropic API)

Avant de tirer le test, vérification que `ANTHROPIC_API_KEY` est bien
disponible. Pas hérité du shell de l'utilisateur ; choix retenu :
fichier `.env` à la racine du repo (déjà couvert par `.gitignore`),
chargé via `set -a; source .env; set +a` avant chaque invocation.
Robuste, réutilisable d'une session à l'autre, pas de fuite dans le
transcript.

Prompt de validation, multi-step pour exercer toute la mécanique
(piped en stdin sur le CLI avec `--reset`) :

> *List the available levels and wall types, then create a wall 5 m
> long on level N00 starting at (0, 0) going east along the x-axis,
> height 2.7 m. Finally, count the walls in the project.*

Sonnet 4.6 a émis **4 tool_use blocks en séquence** sur 4 round-trips :

1. `catalog_list_levels` → renvoie les 2 levels seedés (N00, N01).
2. `catalog_list_wall_types` → renvoie STD200.
3. `walls_create(level_ref="level_001", wall_type_ref="walltype_001",
   p1=[0,0], p2=[5,0], height=2.7)` → wall_001 créé.
4. `aggregations_count("Wall")` → 1.

Réponse finale : tableau Markdown récapitulatif. Stop reason
`end_turn` propre.

KG persisté inspecté post-run :

- `wall_001` avec attrs corrects (`length=5.0`, `height=2.7`,
  `p1=[0,0]`, `p2=[5,0]`, refs résolues).
- 2 arêtes (`at_level` + `is_type`) posées par `walls_create`.
- 4 entrées dans `action_log` (3 du bootstrap turn 0 + 1 wall turn 1).
- Lifecycle intact : `created_at_turn=1`, `modified_at_turn=[]`,
  `deleted_at_turn=None`.

Coût et tokens :

- 4 API calls, 7054 input tokens, 663 output tokens.
- `cache_creation_input_tokens=0`, `cache_read_input_tokens=0` —
  attendu : préfixe rendu (system + tools) sous le seuil Sonnet 4.6
  de 2048 tokens. Cache armé via `cache_control` mais n'aura d'effet
  qu'avec un LLM.md + catalogue plus volumineux. À vérifier de
  nouveau quand on inline le corpus réglementaire UC8 ou un LLM.md
  sérieux.
- Coût estimatif (Sonnet 4.6 à 3 $/M in + 15 $/M out) : ~0.03 $
  pour ce tour.

**Slice validé end-to-end** : registry → schéma → API call →
multi-turn loop → dispatcher → KG transaction → persistence. La
mécanique tient. Prêt pour les phases V0 Semaine 1 (binding Revit
réel via PyRevit) une fois l'accès Revit confirmé.

### État final & reste à faire

**Slice fonctionnel local :**

- KG typé persisté avec rollback atomique ✓
- Registry auto-peuplé par convention docstring ✓
- Dispatcher avec tool_use → tool_result + KG sync ✓
- Boucle multi-turn manuelle avec prompt caching ✓
- 5 tools tier-1 réalistes ✓
- CLI REPL ✓
- 27 tests verts ✓

**Reste à faire avant Semaine 1 V0 (§9 du design doc) :**

- Test live end-to-end avec un vrai appel Anthropic — confirme que la
  boucle multi-turn marche réellement et que Sonnet 4.6 choisit les
  bons tools sur des prompts type « crée un mur de 5m sur le RDC ».
- Récupérer la version PyRevit / CPython3 demain et lever
  l'interdiction des features 3.10+ si possible.
- Bootstrapper l'extension PyRevit (`claude-in-revit.extension/.tab/.panel/.pushbutton`)
  une fois l'accès Revit confirmé, en réutilisant `lib/` tel quel.
- Ajouter `config.py` (clé API depuis `~/.config/claude-in-revit/api_key`,
  fallback env var).
- Ajouter `kg_sync.py` (full re-scan Revit → reconstruct KG) +
  `revit_primitives.py` (transactions, lookups, conversions unités) +
  le décorateur `@kg_synced` qui rend la transaction Revit + KG
  réellement atomique.
- Petite dette : `ProjectKG.load(path)` devrait peupler `persist_path`
  automatiquement (le CLI compense pour l'instant).

**Couverture de risque (§10 du design doc) :**

- LLM hallucination géométrique : non couvert encore (préview/undo
  Revit pas en place).
- KG drift : non couvert (pas de `refresh_kg` puisque pas de Revit).
- Coût API : caching armé, Haiku triage à câbler en V0.
- Tests : ✓ pour la mécanique, ✗ pour les flows LLM réels (pas de
  golden files de prompts).
