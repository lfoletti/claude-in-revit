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
