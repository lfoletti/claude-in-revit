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
