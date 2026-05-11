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
