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
`README.md`, `DESIGN.md`. À reloader côté Revit pour que le ribbon
prenne le nouveau nom (Reload depuis la ribbon pyRevit ou redémarrer
Revit).

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
