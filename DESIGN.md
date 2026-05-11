# claude-in-revit — Design Document

Agent LLM intégré à Autodesk Revit. L'utilisateur prompte en langage naturel,
le LLM orchestre des outils Revit via tool use (Anthropic Claude API), produit
ou modifie un modèle BIM. Pendant Inspirée du planmaker InDesign existant mais
exploitant la richesse sémantique de Revit (Wall Types, hosted elements,
levels, rooms…) qui élimine 80% des problèmes de géométrie 2D rencontrés sur
InDesign.

## 1. Vision et objectifs

**Vision** : un assistant conversationnel architectural qui parle BIM, pas
géométrie 2D. L'utilisateur exprime une intention ("dessine un T3 de 80 m²" /
"déplace toutes les fenêtres du R+2 à 1 m d'allège" / "quantifie les murs
porteurs"), le LLM réalise via un catalogue de tools.

**Objectifs**
- Offrir une UX uniforme : un seul prompt, le LLM dispatche.
- Tirer parti des structures BIM Revit (Wall Types, levels, rooms, hosted) —
  ne pas réinventer la géométrie.
- Maintenir un graphe projet (KG) pour économiser tokens et permettre des
  requêtes complexes (filtres, similarités, historique).
- Conception scalable : prêt pour 100+ tools sans friction.

**Non-objectifs**
- Génération photoréaliste / rendering (Veras, Lumion font ça).
- Substitution complète à l'expertise d'un architecte (l'agent est un
  assistant, pas un décisionnaire).
- Plugins multi-utilisateur synchrone (le KG est local par session).

## 2. Use cases ciblés

| ID | Description | Difficulté |
|----|-------------|-----------|
| UC1 | DWG plan + coupe → modèle 3D | Moyenne (heuristiques classification) |
| UC2 | Programme texte ("T3 80 m²") → modèle 3D | Élevée (créativité spatiale) |
| UC3 | Modification conversationnelle d'un projet existant | Élevée (résolution référentielle) |
| UC4 | Extraction de quantitatifs depuis un projet | Faible (read-only) |
| UC5 | Outline + programme → modèle 3D | Élevée (UC1 + UC2) |
| UC6 | Esquisse / image → modèle 3D | Élevée (vision LLM, plus risqué) |
| UC7 | Modifications en bulk via filtre | Moyenne (find + apply) |
| UC8 | Audit de conformité réglementaire (règlement communal, code travail, code construction…) → rapport de non-conformités localisées | Élevée (RAG + cross-référence KG ↔ corpus + raisonnement multi-règles) |

Tous unifiés derrière une seule entrée prompt + un catalogue de tools.

**Note UC8** : l'agent s'appuie sur une base de connaissance externe (corpus
réglementaire fourni par l'utilisateur ou l'équipe — wiki communal synthétisé,
sections du code du travail, etc.) qu'il croise avec le KG projet pour produire
un rapport structuré : pour chaque règle pertinente, valeur prescrite vs valeur
observée, élément(s) Revit en cause, citation de la source. Voir §4.4 pour le
modèle de la KB réglementaire et §5 pour `compliance.py`.

## 3. Architecture

### Stack
- **PyRevit** (Python 3, runtime CPython3 récent).
- **Anthropic Claude API** via tool use (Sonnet 4.6 par défaut, Haiku 4.5 pour
  triage des opérations triviales).
- **NetworkX** pour les Knowledge Graphs en mémoire.
- **ezdxf** pour parsing DWG/DXF natif.

### Entry point unique

```
LLM.tab/
└── agent.panel/
    ├── prompt.pushbutton/        # UNIQUE entrée conversationnelle
    │   └── script.py
    ├── globals.pushbutton/       # config hors-LLM
    │   └── script.py
    └── refresh_kg.pushbutton/    # sync forcée KG ↔ Revit
        └── script.py
```

Le bouton `prompt` ouvre un dialogue WinForms. L'utilisateur tape sa demande.
Le LLM choisit dans tous les tools disponibles selon le contexte. Tout le
reste de la logique est partagé.

### Arborescence complète

```
claude-in-revit.extension/
├── LLM.tab/
│   └── agent.panel/
│       ├── prompt.pushbutton/script.py
│       ├── globals.pushbutton/script.py
│       └── refresh_kg.pushbutton/script.py
├── lib/
│   ├── llm_api.py             # HTTP, caching, streaming, retry
│   ├── llm_protocol.py        # collecte tools, schema JSON, dispatch
│   ├── project_kg.py          # NetworkX wrapper, schema, persistence
│   ├── kg_sync.py             # full re-scan Revit → reconstruct KG
│   ├── tool_kg.py             # KG logiciel (V1+, scaffold-ready V0)
│   ├── dwg_reader.py          # ezdxf wrapper, extraction entités
│   ├── dwg_classifier.py      # heuristiques (mur / porte / fenêtre)
│   ├── compliance_kb.py       # chargement corpus réglementaire, index, citations
│   ├── compliance_investigations/   # primitives déterministes (hauteur, éclairement, PMR…)
│   ├── revit_primitives.py    # transactions, lookups, conversions unités
│   ├── context.py             # build context (KG queries + catalogue)
│   ├── routing.py             # keyword routing tier-1 / tier-2
│   ├── config.py              # ~/.config/claude-in-revit/
│   └── tools/
│       ├── __init__.py        # auto-import + registry
│       ├── input.py           # import_dwg, import_image, import_outline
│       ├── walls.py           # create, modify, delete, change_param
│       ├── openings.py        # create_door, create_window, set_sill_height
│       ├── rooms.py           # create, recompute_boundaries, set_name
│       ├── levels.py          # create, modify_elevation, list
│       ├── transforms.py      # move, rotate, mirror, scale, copy
│       ├── query.py           # find_by_name, find_in_region, find_similar
│       ├── catalog.py         # list_wall_types, list_family_types
│       ├── bulk.py            # apply_to_filter, change_param_bulk
│       ├── aggregations.py    # count, sum_area, group_by, format_table
│       └── compliance.py      # search_rules, get_rule, audit, list_corpora
├── LLM.md                     # protocole LLM (lu par dispatcher)
├── extension.json             # metadata PyRevit
└── README.md
```

## 4. Modèles de données

### 4.1 KG projet (first-class V0)

Représentation graphique de l'état du modèle Revit, maintenue en parallèle.

**Nodes**
- `Level` : id, name, elevation, llm_id
- `Wall` : id, llm_id, type_ref, level_ref, p1, p2, length, height,
  created_at_turn, modified_at_turn[]
- `Door` / `Window` : id, llm_id, type_ref, host_wall_ref, position,
  sill_height, head_height
- `Room` : id, llm_id, name, area, level_ref, boundary_walls[],
  `use_subcategory` (grain fin : "séjour" / "chambre" / "cuisine" / etc.,
  optionnel)
- `WallType` : id, name, layers_summary, total_thickness (lookup-only)
- `FamilyType` : id, family_name, type_name, dimensions (lookup-only)
- `Phase` : id, name, turn_range (pour step mode)
- `ProjectContext` : singleton, métadonnées projet pour audits & scope —
  voir §4.5

**Edges typés**
- `at_level` : Wall/Door/Window/Room → Level
- `is_type` : Wall → WallType, Door/Window → FamilyType
- `hosts` : Wall → Door/Window
- `bounded_by` : Room → Wall (multi-edges)
- `connects_at` : Wall ↔ Wall (attribut : corner / T-junction / cross)
- `derived_from` : Element → Element (lignée pour copies/symétries)
- `modified_at_action` : Element → action log entry

**Lifecycle**

À la création d'un élément :
```
[Action create_*] →
    [Transaction Revit OPEN]
        ├─ Revit.Wall.Create(...) → ElementId
        ├─ KG.add_node(type=Wall, attrs={...created_at_turn=N...})
        ├─ KG.add_edge(wall → level, "at_level")
        └─ KG.add_edge(wall → wall_type, "is_type")
    [Transaction Revit COMMIT]
    [KG.persist() → disque]
```

À la modification : Revit modifie + KG met à jour les attrs + log event dans
historique action.

À la suppression :
- **Soft delete** par défaut : flag `deleted_at_turn=N` posé, le nœud reste.
- Tool `kg.purge_deleted(older_than=10)` pour nettoyer manuellement.
- Purge auto silencieuse à 50 tours (plafond raisonnable d'une session).

**Idempotence et atomicité**
- Si Revit échoue, ne pas toucher au KG.
- Si KG échoue (rare : disque plein, lock), rollback la transaction Revit.
- Décorateur `@kg_synced` factorise sur tous les tools d'écriture.

**Pré-existants Revit (avant le plugin)**
- Pas dans le KG par défaut. Le LLM ne les voit pas.
- Tool `kg.import_existing(filter)` : scan Revit + ajout au KG avec
  `imported_at_turn=N` (vs `created_at_turn`).

**Drift et sync**
- L'utilisateur édite hors pipeline → KG diverge.
- Détection : avant chaque tool de query lourd, comparaison attrs KG ↔ Revit
  pour les nœuds touchés. Mark `dirty=true` si écart > epsilon.
- Resync : bouton `refresh_kg.pushbutton` ou tool `kg.refresh()`.

**Persistence**
- Local : `~/.config/claude-in-revit/projects/<project_uuid>.kg.json`
- Companion file : `<MonProjet>.claude-in-revit.kg.json` à côté du `.rvt`, optionnel,
  pour partage entre collaborateurs.
- Format JSON, compact mais lisible (debug facile).
- Écriture après chaque action (= chaque modification du KG).

**Granularité historique : par action** (pas par tour). Plus fin, surcoût
mémoire marginal.

### 4.2 KG logiciel (scaffold V0, activation V1+)

Représentation graphique du catalogue de tools.

**Nodes**
- `Tool` : name, description, signature, category, deprecated_flag
- `Concept` : keyword/intent ("déplacement", "filtrage", "ouverture", "métré"…)
- `ToolCategory` : input, walls, openings, query, bulk…

**Edges**
- `Tool → Category` : appartenance
- `Tool → Concept (weight)` : pertinence sémantique
- `Tool → Tool (similar)` : alternatives proches
- `Tool → Tool (compose: out → in)` : chaînable
- `Tool → Tool (preferred_over)` : versions plus spécifiques

**Construction** : auto-générée au chargement du plugin par introspection
des fichiers `tools/*.py` (parsing docstrings selon convention).

**Convention docstring obligatoire**
```python
def set_window_sill_height(filter: dict, new_height_mm: float) -> dict:
    """Modifie la hauteur d'allège des fenêtres correspondant au filtre.

    Concepts: ouverture, fenêtre, allège, hauteur, modification, bulk
    Phrases: "lève les allèges", "passe les fenêtres à X cm",
             "fenêtres rdc à 90 cm de hauteur"
    Similar: change_param (plus générique), set_lintel_height (linteaux)

    Args:
        filter: critères de sélection (level, type, position)
        new_height_mm: nouvelle hauteur en mm réels

    Returns:
        {"ok": bool, "modified_ids": [...], "skipped": [...]}
    """
```

**Activation V1+** : meta-tools exposés au LLM
- `find_tools(intent: str, limit=5)` : recherche sémantique
- `find_similar_tools(tool_name: str)` : alternatives
- `find_tool_chain(from_state, to_state)` : composition

**Bénéfice estimé** : gains massifs sur les tokens dès qu'on dépasse 100
tools (V0 = 50 tools, marginal mais propre à scaffold dès le départ).

### 4.3 KG user (différé V2+)

Mémoire utilisateur persistante : conventions de nommage observées, types
préférés, niveaux par défaut, pattern d'usage. Permet à l'agent de
personnaliser ses suggestions.

Pas de design détaillé en V0.

### 4.4 KB réglementaire (UC8 compliance)

Base de connaissance externe consultée par l'agent pour l'audit de conformité.
Découplée du KG projet : un corpus = un texte réglementaire (règlement
communal, section du code du travail, norme accessibilité, code construction
local, etc.). Plusieurs corpus peuvent coexister et être combinés sur un même
projet (ex : règlement communal + code travail si le projet est un ERP).

**Format du corpus**
- Fichiers Markdown structurés. Un fichier = un texte réglementaire
  cohérent (un règlement, un chapitre).
- Front-matter YAML obligatoire :

```yaml
---
id: rcv-2024            # identifiant court, unique
title: Règlement communal de Vevey
jurisdiction: CH-VD-Vevey
version: "2024-03"      # date de version, pour signaler obsolescence
scope: [logement, ERP]  # types de projet concernés
source: https://...     # URL ou référence papier
---
```

- Chaque section `##` (ou plus profonde) est une *règle unitaire* citable
  par ancre : `rcv-2024#hauteur-sous-plafond`.
- Conventions de balisage léger pour faciliter le RAG et l'extraction de
  valeurs prescrites :
  - Tableau de seuils chiffrés quand applicable (`min`, `max`, condition).
  - Tag `scope:` en début de section pour scope plus fin que le front-matter.

**Localisation**
- Globale : `~/.config/claude-in-revit/compliance/<corpus_id>/*.md`
- Companion projet : `<MonProjet>.compliance/` à côté du `.rvt` pour les
  corpus spécifiques au site (ex : règlement communal applicable).
- Au chargement, fusion globale + companion. Companion gagne en cas de
  conflit d'`id`.

**Index (auto-généré au chargement)**
- Table `règle_id → {scope, mots-clés, hash}`.
- Mots-clés extraits du titre + premier paragraphe (V0). Embeddings
  vectoriels (V1+ si corpus > ~50 K tokens).
- Persisté dans `~/.config/claude-in-revit/compliance/.index.json`, invalidé
  par hash.

**Stratégie d'accès**
- **V0 — full-text + cache** : si le corpus pertinent fait < 30 K tokens, on
  l'inline dans le contexte LLM derrière un *cache breakpoint* dédié. Hit
  rate élevé (corpus statique sur la durée d'une session).
- **V1+ — RAG** : si corpus > 30 K tokens ou corpus combinés trop gros,
  passage à embeddings + retrieval top-K. Le seuil exact sera empirique.

**Cross-référence avec le KG projet**
- L'audit suit ce schéma par règle :
  1. Sélection des règles pertinentes (filtre par `scope` + intent).
  2. Pour chaque règle : extraction de la valeur prescrite (par le LLM,
     guidé par le balisage de seuils).
  3. Requête KG correspondante (`query.find_*`, `aggregations.*`,
     `walls.get_param`, etc.) pour la valeur observée.
  4. Comparaison + génération d'une entrée de rapport si écart.
- Le rapport référence à la fois l'ancre de règle (`rcv-2024#…`) et les
  `llm_id` des éléments KG concernés → l'utilisateur peut zoomer sur
  l'élément Revit en un clic depuis l'UI.

**Versionnement et fraîcheur**
- Champ `version` du front-matter affiché dans tout rapport.
- Warn UI si une règle citée a `version` > 12 mois et le projet est en
  phase active (réglementation susceptible d'avoir évolué).

**Hors-scope V0**
- Mise à jour automatique du corpus depuis une source en ligne (scraping
  d'un wiki communal, par ex.) — repoussé V1+.
- Validation juridique automatique : l'agent ne se prononce **pas** sur
  les ambiguïtés. Toute règle interprétée comme « probablement non
  applicable » est listée séparément, pour décision utilisateur.

### 4.5 ProjectContext (métadonnées projet pour audits)

Les primitives d'audit sont creuses sans contexte projet : « hauteur sous
plafond minimale » dépend de l'usage de la pièce, « distance de fuite »
dépend de la catégorie AEAI du bâtiment, « ratio sanitaires » dépend de
l'effectif et de l'ouverture au public. Ces métadonnées ne sont **ni
géométriques ni dérivables du seul modèle Revit** — elles relèvent d'une
déclaration projet.

**Modèle hiérarchique (3 niveaux)**

| Niveau | Porteur | Cas d'usage |
|--------|---------|-------------|
| Projet | `ProjectContext` (singleton, V1) | Bâtiment mono-affectation, métadonnées globales (catégorie AEAI, hauteur classe, effectif total, ouverture au public…) |
| Compartiment | `Compartment.affectation` (V2) | Bâtiment mixte : rez commerce + étages logement, atelier + bureaux administratifs… |
| Pièce | `Room.use_subcategory` (V1, optionnel) | Grain fin à l'intérieur d'une affectation : "séjour" / "chambre" / "cuisine" dans un logement, déterminant pour des seuils différenciés |

Le scope d'une règle est résolu en lookup descendant : `Room` → si
`use_subcategory` set, l'utiliser ; sinon → `Compartment.affectation` si
défini ; sinon → `ProjectContext.building_category`.

**Schéma `ProjectContext`** (singleton, id fixe `"project_context"`)

```python
ProjectContext {
    # Affectation et public
    building_category: str       # AEAI : "logement" | "bureau" | "industriel"
                                 # | "restaurant" | "hôtel" | "ERP_assemblée"
                                 # | "ERP_vente" | "santé" | "scolaire" | …
    is_public_access: bool       # ouvert au public ? (déclenche scope ERP)
    mixed_use: bool              # si vrai → s'attendre à des Compartment overrides

    # Géométrie hors-modèle
    height_class_aeai: str       # "faible" (< 11 m) | "moyenne" (11–30 m)
                                 # | "élevée" (30–100 m) | "haute" (> 100 m)
    floors_above_ground: int
    floors_below_ground: int

    # Effectif (peut être estimé ou déclaré)
    total_occupants_estimated: int | None
    occupants_source: "declared" | "computed_from_area" | None

    # Corpus actifs
    applicable_corpora: list[str]   # [corpus_id, …] — alimente §4.4

    # Provenance par champ (dict parallèle)
    _provenance: dict[field_name, FieldProvenance]
}

FieldProvenance {
    source: "user_declared" | "inferred" | "default"
    confidence: "high" | "medium" | "low"
    set_at_turn: int
    set_by: "user" | "agent_inference" | "system_default"
    notes: str | None    # ex : "déduit du nom de projet 'Restaurant X'"
}
```

**Provenance — pourquoi c'est crucial**

Un champ `inferred` à confidence `medium` doit être confirmé par
l'utilisateur avant tout audit blocking. Un champ `user_declared` à
`high` est tenu pour acquis. Cette distinction empêche l'agent de
produire un rapport « X non conforme » alors qu'il a deviné le contexte
qui rend X applicable.

**Pré-requis par primitive**

Chaque primitive (§5) déclare ses dépendances de contexte :

```python
@investigation(
    handles=["*#hauteur-sous-plafond"],
    requires_context=["building_category", "Room.use_subcategory?"]
)
def ceiling_height(...): ...
```

`requires_context` accepte la notation `Field` (obligatoire) ou `Field?`
(préférable mais non bloquant — fallback sur niveau supérieur).

**Interview pré-audit (`compliance.gather_context`)**

Quand un audit est lancé et qu'il manque des champs requis :

1. `audit()` calcule l'union des `requires_context` des primitives à
   exécuter.
2. Diff avec ce qui est déjà dans `ProjectContext` (en respectant
   `confidence ≥ medium` pour les champs critiques).
3. Si gaps → tool `compliance.gather_context(missing_fields)` exposé au
   LLM, qui pose les questions à l'utilisateur de manière structurée
   (pas un dialogue libre, une liste de questions ciblées).
4. Réponses persistées dans `ProjectContext` avec `source: user_declared`,
   `confidence: high`.
5. Audit reprend.

Re-audit ultérieur → ne re-pose pas les questions sauf si l'utilisateur
demande explicitement `compliance.update_context(field, value)` ou
`compliance.reset_context()`.

**Inférence préliminaire (option)**

Au premier appel d'audit, l'agent peut **proposer** des valeurs inférées
depuis le KG / nom de projet / nom des Family Types (ex : présence
massive de Family `LLM_chambre_*` → suggère `building_category=logement`).
L'utilisateur valide ou corrige. Toute valeur inférée non confirmée
reste à `confidence: low` et déclenche l'interview au prochain audit.

**Mise à jour et invalidation**

- Modifier un champ de `ProjectContext` invalide les rapports d'audit
  qui dépendaient de l'ancienne valeur. Marquer rapport `stale=true`
  plutôt que les supprimer (utilisateur peut comparer).
- Un changement de `building_category` est rare mais lourd : prompt
  utilisateur de confirmation explicite (« vous changez l'affectation
  du projet, X règles vont devenir applicables, continuer ? »).

## 5. Tools

### Catégories (10 fichiers, ~50 tools en V0)

| Fichier | Tools typiques | Catégorie LLM |
|---------|---------------|----------------|
| `input.py` | import_dwg, import_image, import_outline, kg.import_existing | I/O |
| `walls.py` | create, modify, delete, change_param, get_param | Création/édition |
| `openings.py` | create_door, create_window, set_sill_height, set_lintel_height | Création/édition |
| `rooms.py` | create, recompute_boundaries, set_name, get_area | Création |
| `levels.py` | create, modify_elevation, list, set_active | Création |
| `transforms.py` | move, rotate, mirror, scale, copy | Édition |
| `query.py` | find_by_name, find_in_region, find_similar, neighbors, history, diff_since | Lecture KG |
| `catalog.py` | list_wall_types, list_family_types, project_units | Lecture projet |
| `bulk.py` | apply_to_filter, change_param_bulk | Édition en masse |
| `aggregations.py` | count, sum_area, sum_length, group_by, format_table | Métré |
| `compliance.py` | list_corpora, search_rules, get_rule, audit, report_violation, gather_context, update_context, get_context | Conformité réglementaire |

**Détail tools `compliance.py`**

| Tool | Signature (allégée) | Rôle |
|------|---------------------|------|
| `list_corpora()` | `→ [{id, title, version, scope, jurisdiction}]` | Inventaire des corpus chargés (global + companion projet). |
| `search_rules(query, corpus=None, scope=None, limit=10)` | `→ [{rule_id, title, excerpt, score}]` | Recherche dans l'index ; renvoie extraits citables avec ancre. |
| `get_rule(rule_id)` | `→ {rule_id, title, body, source, version}` | Texte complet d'une règle pour raisonnement détaillé. |
| `audit(scope_filter, corpora=None)` | `→ {report: [...], summary: {...}, context_gaps: [...]}` | Tour orchestré : check `ProjectContext` → gather_context si gaps → sélection des règles applicables × primitives × agrégation. |
| `report_violation(rule_id, observed, prescribed, element_ids, severity, method_id)` | `→ {ok}` | Append d'une entrée structurée au rapport (consommée par l'UI/export). |
| `gather_context(missing_fields, suggestions=None)` | `→ {answers: {...}, deferred: [...]}` | Pose à l'utilisateur les questions nécessaires aux primitives (typologie, ouverture au public, effectif…). Suggestions inférées proposées en pré-remplissage. Persisté dans `ProjectContext`. |
| `update_context(field, value, confidence='high')` | `→ {ok, invalidated_reports: [...]}` | Mise à jour explicite d'un champ de contexte. Marque les rapports impactés comme `stale`. |
| `get_context()` | `→ ProjectContext` | Lecture du contexte courant + provenance par champ. |

L'audit est **orchestré par le LLM lui-même** : `audit()` initialise un
rapport vide et expose un sous-protocole où le LLM itère
`search_rules` → `get_rule` → tools KG (`query.*`, `aggregations.*`,
`walls.get_param`…) → `report_violation`. Cette boucle réutilise le tool-use
multi-turn standard ; pas d'orchestrateur dédié à coder.

Format de sortie d'une violation (consigné aussi dans le KG projet, edge
`violates` entre élément et règle) :

```json
{
  "rule_id": "rcv-2024#hauteur-sous-plafond",
  "rule_title": "Hauteur sous plafond minimale (logement)",
  "severity": "blocking | warning | info",
  "prescribed": {"value": 2.40, "unit": "m", "condition": "pièce de jour"},
  "observed": {"value": 2.30, "unit": "m"},
  "elements": [{"llm_id": "room_07", "revit_id": 184221, "name": "Séjour"}],
  "citation": "Art. 12.3 — « La hauteur minimale… »",
  "version": "2024-03",
  "method": "ceiling_height_primitive_v1"
}
```

### Méthodes d'investigation (playbook d'audit)

Un audit purement LLM-driven n'est pas reproductible : pour la même question
réglementaire et le même projet, on veut **la même réponse à chaque exécution**.
D'où une architecture **hybride** :

- **Primitives d'investigation** (Python pur, déterministes) pour les familles
  d'audits récurrentes et critiques. Chacune lit le KG, applique une formule
  bien définie, retourne valeur observée + métadonnées (méthode utilisée,
  données manquantes, hypothèses prises). Reproductibles, unit-testables.
- **Fallback LLM** pour les règles non couvertes par une primitive : la boucle
  tool-use standard (`search_rules` → `get_rule` → tools KG → `report_violation`)
  prend le relais. Le rapport indique alors `method: "llm_inferred_v1"` au
  lieu d'une primitive nommée — **signal de moindre confiance** pour
  l'utilisateur.

**Localisation** : `lib/compliance_investigations/` (un fichier par famille,
introspecté comme les tools). Chaque primitive est mappée à un ou plusieurs
`rule_id` ou patterns d'`id` (front-matter de la règle ou tags).

```python
@investigation(handles=["*#hauteur-sous-plafond", "*#ceiling-height"])
def ceiling_height(kg, scope) -> InvestigationResult:
    """Calcule la hauteur sous plafond par Room dans le scope.
    Méthode : level(N+1).elevation - level(N).elevation - slab_thickness.
    Données manquantes signalées explicitement (pas de silence).
    """
    ...
```

`compliance.audit()` enchaîne :

1. **Check ProjectContext** (§4.5) : union des `requires_context` des
   primitives à exécuter ; si gaps → `gather_context()` (interview
   utilisateur) ; sinon → étape suivante.
2. **Sélection des règles** : filtre par `applicable_corpora` × scope
   résolu via la hiérarchie `Room.use_subcategory` → `Compartment.affectation`
   → `ProjectContext.building_category`.
3. **Dispatch** : pour chaque règle, lookup d'une primitive. Si trouvée
   → exécution déterministe. Sinon → cession au LLM avec le contexte de
   la règle.
4. **Agrégation** : violations + `data_gaps` (données KG manquantes)
   + `context_gaps` (champs ProjectContext encore à `confidence: low`).

**Familles d'audits principaux (V1 cible — 6 primitives)**

| Famille | Données KG / inputs requis | Méthode | Pièges connus |
|---------|----------------------------|---------|---------------|
| Hauteur sous plafond | `Room` × `Level` × épaisseur dalle | `level(N+1).elev − level(N).elev − slab_thickness` ; Min sur la pièce si dalle inclinée | Faux plafonds non modélisés ; mezzanines ; rampes ; sous-pentes (combles) |
| Surface d'éclairement naturel | `Room.area` + `Window` hosted dans murs frontières | `Σ(window.width × window.height) / Room.area` ; ratio min selon usage | Loggias/brise-soleil non décomptés ; courettes non équivalentes à façade ; impostes vs allèges pleines |
| Largeur de passage (porte/circulation) | `Door.width` ; `Room` typée couloir + géométrie | Pour porte : `Door.width − jeu battant` ; pour couloir : largeur min sur l'axe court | Passage utile vs libre (battant ouvert 90°) ; rétrécissements ponctuels ; poteaux ; montants épais |
| Hauteur d'allège | `Window.sill_height` (attribut natif V0) | Comparaison directe au seuil prescrit, conditionné par usage de la pièce hôte | Fenêtres de toit (plan incliné) ; allèges variables sur grandes baies ; garde-corps rapportés |
| Accessibilité PMR — porte | `Door.width`, `Door.threshold_height` | Largeur ≥ 90 cm + ressaut ≤ 2 cm (norme typique) | Portes coulissantes vs battantes ; double vantail (passage utile cumulé ?) ; portes techniques exclues |
| Surfaces habitables / SHON | `Room.area` + classification d'usage | `Σ Room.area` filtrée par usage prescrit ; éventuellement pondérée (sous-pente < 1.80 m) | Définitions divergentes (SHON / SHAB / surface utile) ; locaux annexes ; pièces sous combles |

**Familles secondaires (V2)** : largeur d'escalier (giron + emmarchement),
distances aux limites parcelle (pré-requis : limite tracée), hauteur de
bâtiment au faîte/égout, ratio sanitaires/effectif (code travail, effectif
en input externe), surface par poste de travail (code travail, mobilier
rarement modélisé en BIM standard).

**Famille Protection Incendie (V2 — bloc à part)**

La PI est une famille à elle seule : algorithmiquement plus lourde (graphes
de fuite, compartimentage) et requiert des extensions du KG projet
(attributs de résistance au feu, type `Compartment`).

**Référentiel pilote V2 : AEAI** (Association des établissements cantonaux
d'assurance incendie, Suisse) — *Norme de protection incendie* + *Directives
de protection incendie* (DPI) numérotées. Citations dans le rapport sous la
forme `aeai-2015#dpi-15-15-§3.2` (corpus_id + ancre directive + paragraphe).
Les autres référentiels (ERP/IGH France, normes EN harmonisées) sont
adressables ultérieurement via des corpus supplémentaires sans toucher aux
primitives, à condition que les primitives restent paramétrées (les seuils
sortent de la règle, pas du code).

Conséquence d'architecture : les primitives PI ne **codent pas** les seuils
AEAI en dur. Elles extraient les seuils du corpus actif (front-matter ou
balisage de la DPI) et appliquent la méthode. Une primitive « distance de
fuite » fonctionne identiquement sur AEAI (35 m typ. en compartiment unique)
ou sur un référentiel français (40 m typ.) — seul le seuil prescrit change.

| Primitive | Données KG / inputs requis | Méthode | Pièges connus |
|-----------|----------------------------|---------|---------------|
| Distance de fuite | Graphe `Room ↔ Door ↔ Room`, `Door.is_exit`, géométrie pièces | Plus court chemin de tout point d'une pièce vers la sortie la plus proche (Dijkstra sur graphe pondéré par distances réelles, pas Manhattan) | Distance en ligne droite vs cheminement réel ; obstacles (mobilier, cloisons amovibles) ; longueur cumulée vs longueur jusqu'à embranchement |
| Typologie des portes | `Door.fire_rating` (EI30/EI60/EI90/RF…), `Door.is_smokeproof`, contexte de pose (compartiment, escalier, gaine) | Pour chaque porte, vérifier que sa classification ≥ celle prescrite par le contexte (cloison entre compartiments → EI60 typ., porte de cage d'escalier → EI30 + ferme-porte) | Classification souvent absente du modèle BIM standard → input manuel ou convention de nommage Family ; portes de service vs portes d'issue |
| Largeur d'issue (AEAI : largeur utile par 100 personnes) | `Door.width` (passage utile) cumulée par compartiment, effectif théorique du compartiment | Largeur utile cumulée des issues d'un compartiment ≥ seuil prescrit selon affectation et effectif (AEAI raisonne en largeur utile minimale, pas en UP de 0.60 m comme la France) | Effectif théorique non déductible directement du KG (input ou calcul par usage × surface) ; portes à double vantail (largeur cumulée) ; portes condamnables exclues ; seuil dépendant de l'affectation (logement / bureau / ERP) |
| Distance entre issues alternatives | Graphe de fuite + flag `Door.is_exit` | Pour chaque pièce avec ≥ 2 issues, angle entre les directions de fuite (règle dite « du tiers » ou équivalent local) | Définition variable selon référentiel ; pertinence uniquement au-delà d'un effectif seuil |
| Compartimentage (continuité coupe-feu) | Type `Compartment` (V2), `Wall.fire_rating`, `Door.fire_rating` sur les frontières | Pour chaque frontière entre deux compartiments, vérifier que **tous** les éléments traversants (murs, portes, gaines) atteignent la résistance prescrite | Trémies / pénétrations non modélisées ; faux plafonds non coupe-feu ; gaines techniques ; clapets non représentés |
| Désenfumage (surface ouvrante / surface au sol) | `Window.is_smoke_vent` ou flag équivalent, `Room.area` | `Σ surface_ouvrante_désenfumage / Room.area` ≥ ratio prescrit, par compartiment ou par cage | Désenfumage mécanique vs naturel (logiques différentes) ; cages d'escalier ; ouvrants en partie haute uniquement |

**Extensions de schéma KG requises pour PI** (à intégrer en V2 quand on
implémente ces primitives) :

- `Wall` : ajout `fire_rating: str | None` (ex : "EI60", "REI90", null si
  non pertinent / non renseigné).
- `Door` : ajout `fire_rating`, `is_smokeproof: bool`, `is_exit: bool`,
  `has_self_closer: bool`.
- `Window` : ajout `is_smoke_vent: bool`, `openable_area_m2: float`.
- Nouveau node `Compartment` : id, name, level_ref, bounded_by[] (walls/slabs).
- Nouveaux edges : `belongs_to_compartment` (Room → Compartment),
  `compartment_boundary` (Wall → Compartment, multi-edges si frontière
  entre 2 compartiments).
- Tool `kg.classify_compartments()` : auto-détection compartiments à partir
  des murs `fire_rating != null` (V2.5, optionnel).

Ces attributs sont *purement informatifs* pour la création (le LLM ne les
infère pas, ils viennent du modèle Revit ou d'une saisie utilisateur
guidée). L'audit signale `data_gaps` quand l'attribut est `null` sur un
élément qui devrait l'avoir selon son contexte (ex : porte sur frontière
de compartiment sans `fire_rating` → gap, pas violation).

**Algorithmes**

- Path-finding distances de fuite : NetworkX (déjà au stack) supporte
  Dijkstra et A\*. Coût en performance acceptable jusqu'à ~10 K nœuds.
- Vérification de continuité coupe-feu : itération sur les arêtes
  `compartment_boundary`, agrégation des `fire_rating` des éléments
  traversants. O(n) sur le nombre de frontières.

**Convention de retour d'une primitive**

```python
@dataclass
class InvestigationResult:
    method_id: str               # "ceiling_height_primitive_v1"
    observations: list[Observation]   # une par élément testé
    data_gaps: list[DataGap]     # éléments non mesurables → pourquoi
    assumptions: list[str]       # hypothèses prises explicitées
```

`data_gaps` est crucial : une primitive qui ne peut pas mesurer (ex : pas
de niveau supérieur défini, donc hauteur sous plafond inconnue) **doit le
dire**, jamais retourner silencieusement. Le rapport agrège ces gaps dans
une section dédiée « règles non vérifiables — données manquantes ».

**Versionnement des primitives** : `method_id` inclut un suffixe `_vN`. Une
mise à jour de la formule incrémente la version → les anciens rapports
restent interprétables, et un re-audit affiche un diff méthodologique si
pertinent.

### Tier-1 vs Tier-2

**Tier-1 (toujours chargés, ~15 tools)** : query.find_by_name,
query.find_in_region, walls.create, walls.change_param, openings.create_door,
openings.create_window, rooms.create, transforms.move, transforms.delete,
catalog.list_wall_types, catalog.list_family_types, levels.list, query.history,
query.neighbors, aggregations.count.

**Tier-2 (chargés selon routing keyword)** : le reste, par groupes.

### Routing keyword-based (`routing.py`)

```python
ROUTING_RULES = {
    "input": ["dwg", "import", "fichier", "esquisse", "image", "photo",
              "polyligne", "outline"],
    "bulk": ["tous les", "toutes les", "ensemble des", "filtre",
             "pour chaque", "tout le", "toute la"],
    "aggregations": ["quantité", "métré", "surface totale", "longueur totale",
                     "nombre de", "comptage", "tableau"],
    "openings_advanced": ["allège", "linteau", "sill", "lintel", "hauteur de"],
    "transforms_advanced": ["miroir", "symétrie", "rotation", "duplique"],
    "compliance": ["conformité", "conforme", "règlement", "réglementaire",
                   "norme", "code du travail", "communal", "audit",
                   "non-conformité", "infraction", "loi", "permis"],
}
```

Quand le groupe `compliance` est activé, le corpus pertinent (filtré par
juridiction + scope projet) est inliné dans le contexte derrière un cache
breakpoint dédié — voir §7.

Détection au début du tour, avant l'appel API. Charge les groupes
correspondants en plus du tier-1.

## 6. Pipeline d'un tour

```
[User entre prompt]
       ↓
[routing.py : analyse du prompt → groupes tier-2 à charger]
       ↓
[context.py : build context]
   ├─ KG.diff_since(turn=N-2)         # changements récents
   ├─ catalog filtré (Wall Types, Families pertinents)
   └─ tier-1 + tier-2 sélectionnés (descriptions de tools)
       ↓
[llm_api.py : appel API Anthropic]
   ├─ system prompt (LLM.md + header dynamique avec état)
   ├─ messages (3 derniers tours en clair)
   ├─ tools (tier-1 + tier-2 actifs)
   └─ cache_control sur LLM.md, catalogue, history-prefix
       ↓
[Multi-turn tool use loop]
   ├─ LLM retourne tool_use → exécute via dispatcher
   ├─ KG mis à jour (atomique avec Revit)
   ├─ résultat retourné au LLM
   └─ continue jusqu'au stop_reason == "end_turn"
       ↓
[Post-processing]
   ├─ KG.persist() → disque
   ├─ caption / log dans le doc Revit
   ├─ append turn dans context.md (~/.config/)
   └─ message succès à l'utilisateur
```

## 7. Économie de tokens

### Stratégie multi-couche

| Levier | Gain typique | Complexité |
|--------|-------------|-----------|
| **Prompt caching** (system + catalogue + history-prefix) | 70-90% sur input répété | Faible |
| **KG diff context** (que les changements depuis tour N-2) | 40-70% sur context | Moyenne |
| **Routing tier-1/tier-2** (pas tous les tools chargés) | 30-50% sur tool defs | Faible |
| **Tool use à la demande** (LLM tire ce qu'il veut) | 50-80% sur context initial | Moyenne |
| **Modèle plus petit (Haiku 4.5) pour ops triviales** | 80-90% sur ces cas | Faible |
| **Trim history après 3 tours** (résumé compact des plus anciens) | 20-40% sur history | Moyenne |
| **Catalogue filtré** (préfixe `LLM_*`, types utilisés, favoris) | 60-80% sur catalogue | Faible |
| **Corpus réglementaire caché** (UC8, statique sur session) | 80-95% sur corpus inliné | Faible |
| **RAG corpus réglementaire** (V1+, gros corpus) | 60-90% sur le corpus selon top-K | Élevée |

### Anthropic prompt caching breakpoints

Jusqu'à 4 cache breakpoints :
1. Fin du LLM.md (statique).
2. Fin du catalogue (Wall Types + Family Types, semi-statique).
3. Fin du corpus réglementaire inliné, **uniquement si UC8 actif** (statique
   sur la session — hit rate quasi 100% sur un audit multi-tour).
4. Début de la conversation (premier user message après catalogue/corpus).

Anthropic facture 10% sur cache hit. TTL 5 min (couvre une session normale).

### Triage par modèle

Heuristique sur le prompt avant appel :
- Mots-clés simples ("déplace", "supprime", "renomme", "compte") + prompt
  court → Haiku 4.5.
- Création générative ("dessine", "génère", "propose") ou prompts longs →
  Sonnet 4.6.

Économie ~85% sur les ops d'édition, qui représentent souvent 40% des tours.

## 8. Persistence et configuration

### Hiérarchie des fichiers de config

```
~/.config/claude-in-revit/
├── api_key                        # clé Anthropic, chmod 600
├── config.json                    # globals (Wall Types par défaut, etc.)
├── context.md                     # historique conversations cross-projet
├── projects/
│   ├── <uuid>.kg.json             # KG projet par projet
│   └── <uuid>.context.md          # historique conversation projet
└── extensions/
    └── claude-in-revit/          # cache de l'extension PyRevit
```

### Identifiant projet
- Tentative 1 : paramètre partagé Revit `claude-in-revit.project_uuid` créé au
  premier run.
- Fallback : hash de `Document.PathName`.

### Companion file optionnel
- `<MonProjet>.claude-in-revit.kg.json` à côté du `.rvt`.
- Permet de partager le KG entre collaborateurs (commit dans Git, par exemple).
- Synchronisation : si companion existe et plus récent que cache local, on
  charge le companion.

## 9. Plan d'implémentation

### V0 (3-5 semaines pour les UC fondationnels)

**Semaine 1 : Foundation**
- Scaffold extension PyRevit + dialog prompt.
- `llm_api.py` : appel Anthropic, prompt caching, retry, error handling.
- `llm_protocol.py` : registry de tools, génération schema JSON, dispatcher
  tool use.
- `project_kg.py` : NetworkX setup, schéma de base, persistence JSON.
- `kg_sync.py` : full scan + bootstrap initial.
- Tools tier-1 minimum : query.find_by_name, walls.create, catalog.list_*.
- **À ce stade** : UC4 (quantitatifs) basique fonctionne.

**Semaine 2-3 : Géométrie complète**
- Tools : walls.modify, walls.delete, openings (door/window create + sill +
  lintel), rooms, levels.
- Transforms : move, rotate, mirror, copy.
- **À ce stade** : UC2 et UC3 fonctionnels.

**Semaine 4-5 : I/O et bulk**
- `dwg_reader.py` + `dwg_classifier.py`.
- Tools input.* et bulk.*.
- **À ce stade** : UC1, UC5, UC7 fonctionnels.

UC6 (vision) après V0 (probablement V1, exige plus de validation qualité).
UC8 (compliance) après V0 : dépend d'un KG projet riche et stable (sinon
trop de faux négatifs sur les règles dimensionnelles).

### V1 (2-3 semaines)
- UC6 (vision) + tools associés.
- UC8 (compliance) — full-text + cache, pas encore RAG :
  - `compliance_kb.py` : chargement Markdown + front-matter, index simple.
  - `tools/compliance.py` : list_corpora, search_rules, get_rule, audit,
    report_violation, **gather_context, update_context, get_context**.
  - **`ProjectContext` (singleton dans le KG)** + `Room.use_subcategory`
    (optionnel) : schéma, persistence, provenance par champ. Voir §4.5.
  - Interview pré-audit fonctionnelle : `gather_context` pose les
    questions de typologie / ouverture au public / hauteur classe AEAI
    / effectif estimé, persiste dans `ProjectContext`, ne re-pose pas
    aux audits ultérieurs.
  - `compliance_investigations/` : 6 primitives V1 (hauteur sous plafond,
    éclairement, largeurs de passage, hauteur d'allège, PMR porte, surfaces
    habitables) avec déclaration `requires_context` et unit tests sur
    projets-fixtures (avec contextes variés).
  - 1 corpus pilote (ex : règlement communal d'une commune type) pour
    valider le format et la boucle d'audit + le mapping primitives ↔ règles.
  - UI rapport : table HTML rendue dans WinForms, lien clic → zoom Revit ;
    section dédiée « données manquantes » alimentée par `data_gaps`.
- KG logiciel meta-tools activés si nécessaire (>80 tools).
- Step mode via Phases Revit.
- Tests sur projets réels et itérations qualité.

### V2 (4+ semaines)
- KG user (project memory persistante).
- UC8 RAG : embeddings + retrieval pour gros corpus (> 30 K tokens) ou
  combinaisons multi-corpus. Possible rapprochement avec les embeddings
  de tool search s'ils sont activés.
- UC8 primitives secondaires : largeur d'escalier, setbacks, hauteur de
  bâtiment, ratio sanitaires (code travail), surface par poste.
- UC8 famille **Protection Incendie** (référentiel pilote : **AEAI**) :
  - Extensions de schéma KG (`fire_rating` sur Wall/Door, node `Compartment`,
    edges associés).
  - Corpus AEAI structuré en Markdown (Norme de protection incendie + DPI),
    convention de citation `aeai-2015#dpi-XX-XX-§Y`.
  - Primitives : distance de fuite (Dijkstra sur graphe pièces-portes),
    typologie des portes coupe-feu, largeur d'issue (largeur utile AEAI,
    pas UP), distance entre issues, compartimentage, désenfumage.
  - Seuils paramétrés depuis le corpus, pas codés en dur dans les primitives
    → portage ultérieur ERP/EN possible sans modifier le code Python.
- Mise à jour automatique du corpus depuis sources en ligne (scraping
  de wikis communaux versionnés, par ex.).
- Optimisations avancées (embeddings vectoriels pour tool search si
  KG logiciel insuffisant, batch optimizations).
- Distribution publique : packaging PyRevit, doc utilisateur, marketing.

## 10. Risques et mitigations

| Risque | Probabilité | Impact | Mitigation |
|--------|------------|--------|-----------|
| LLM hallucination géométrique (positions inventées) | Moyenne | Élevé | Tools de validation (`read_dimension_text_at`), preview avant commit, undo natif Revit |
| KG drift (utilisateur édite hors pipeline) | Élevée | Moyen | Bouton refresh + détection auto via comparaison attrs |
| Coût API à long terme | Moyenne | Moyen | Caching agressif + Haiku triage + monitoring usage par projet |
| Wall Types / Families inexistants ou mal nommés | Élevée | Élevé | Convention `LLM_*` ou `MUR_*` documentée + fallback générique |
| Profusion de tools → confusion LLM | Moyenne | Moyen | Tier-1/Tier-2 + KG logiciel V1+ |
| Coupe DWG mal alignée avec plan | Moyenne | Moyen | Convention "ligne A-A annotée" ou clic manuel utilisateur |
| Performance NetworkX sur gros projets | Faible | Faible | NetworkX scale jusqu'à 100K nodes ; nos projets sont bien en-dessous |
| Tools = LLM doit choisir le bon, dérive | Moyenne | Moyen | Docstrings strictes (concepts, phrases, similar) + KG logiciel |
| **UC8** Corpus réglementaire obsolète (loi modifiée non synchronisée) | Élevée | Élevé | Champ `version` obligatoire, warn UI au-delà de 12 mois, source citée systématiquement |
| **UC8** Faux positifs/négatifs (interprétation LLM d'une règle ambiguë) | Élevée | Élevé | Primitives déterministes pour les audits principaux (`method_id` versionné), citation littérale obligatoire, sévérité graduée, validation utilisateur consignée dans le KG ; fallback LLM signalé `method: "llm_inferred"` (signal de moindre confiance) |
| **UC8** Reproductibilité d'un audit (rejouer = même rapport) | Moyenne | Élevé | Primitives déterministes versionnées (`method_id_vN`), gel du `version` du corpus dans le rapport, données KG consignées au moment de l'audit |
| **UC8** Règle inapplicable au projet (mauvais scope) | Moyenne | Moyen | `ProjectContext` (§4.5) avec provenance par champ, scope résolu hiérarchiquement (Room → Compartment → Project), filtre strict (juridiction, building_category), section « règles écartées » dans le rapport pour transparence |
| **UC8** Audit sans contexte projet → faux silences ou faux positifs | Élevée | Élevé | Interview pré-audit obligatoire (`gather_context`) sur les champs `requires_context` des primitives ; champs à `confidence: low` listés en `context_gaps` du rapport ; pas d'audit blocking si contexte critique manque |
| **UC8** KG projet incomplet → règles dimensionnelles ignorées | Élevée | Moyen | `data_gaps` obligatoire dans chaque primitive : section dédiée du rapport « règles non vérifiables — données manquantes », jamais de silence |
| **UC8** Engagement de responsabilité (l'utilisateur prend l'audit pour avis juridique) | Moyenne | Élevé | Disclaimer en tête de chaque rapport, mention « assistance, pas validation réglementaire » dans le doc et l'UI |

## 11. Décisions ouvertes (à régler en cours de scaffolding)

- **Liste exacte des keywords par groupe** dans `routing.py`. Définir
  empiriquement après les premiers vrais usages.
- **UC6 (vision) avant ou après UC1 (DWG)** : DWG plus fiable, vision plus
  universel. Probablement DWG d'abord (V0), vision en V1.
- **Format précis du system prompt** : LLM.md global + sections par contexte
  chargées dynamiquement, ou tout-en-un avec instructions conditionnelles ?
- **Patterns d'erreur et recovery** : si un tool call échoue, comment le LLM
  est informé (texte d'erreur structuré ? exception ?), et comment il itère
  pour corriger ?
- **Tests** : stratégie de test (golden files de DWG, scénarios de prompts,
  validation par snapshot du KG).
- **Distribution** : open source (GitHub + license), payant (App Store /
  marketplace Autodesk), ou interne (usage perso/équipe) ?
- **Layer-naming convention DWG** : strict OU auto-détection + UI mapping ?
  Probablement les deux : auto-détection + validation utilisateur avant envoi.
- **Validation utilisateur** : V0 one-shot, V1 step-by-step optionnel.
- **Coupe absente UC1** : warn + valeurs par défaut (h=2.7m, 1 niveau).
- **UC8 — format précis du corpus** : Markdown libre + front-matter (souple)
  vs schéma strict avec champs typés (machine-friendly mais friction d'auteur).
  Probablement Markdown + front-matter en V1, schéma strict optionnel en V2
  pour les seuils chiffrés.
- **UC8 — bascule full-text → RAG** : seuil exact (30 K tokens proposé,
  empirique), unification ou non avec l'index du KG logiciel.
- **UC8 — gestion multi-juridictions** : un projet peut tomber sous plusieurs
  corpus (communal + cantonal + travail). Stratégie de priorité en cas de
  conflit (la plus restrictive ? première chargée ?). À trancher avec un cas
  réel.
- **UC8 — sortie du rapport** : table dans WinForms (rapide à coder), export
  PDF (utile en production), commentaires posés dans Revit (intégration
  forte mais bruyant). Probablement les trois, par étapes.
- **UC8 — consignation des violations dans le KG** : edge `violates` avec
  attribut `acknowledged_by_user_at_turn` pour mémoriser les acceptations,
  ou rapport externe sans persistence ? Penche vers consigner — utile pour
  les itérations de design.
- **UC8 — vocabulaire fermé pour `building_category`** : liste figée
  (alignée AEAI) ou texte libre + normalisation par l'agent ? Liste
  fermée pour la fiabilité du scope, mais doit couvrir les cas mixtes.
  Probablement enum + champ `notes` libre.
- **UC8 — inférence de contexte depuis le KG** : à quel point l'agent
  doit-il *proposer* (noms de Family Types, nom de projet) vs uniquement
  demander ? Compromis confiance / friction utilisateur, à calibrer
  empiriquement.

## 12. État de validation au moment de ce doc

Décisions formellement validées :
- ✅ Stack PyRevit + Python 3
- ✅ Single entry point conversationnel
- ✅ 8 use cases unifiés derrière le même socle (UC8 compliance ajouté)
- ✅ KG projet first-class V0, NetworkX, JSON, companion file pour partage
- ✅ Lifecycle : created_at_turn, soft delete, purge auto à 50 tours
- ✅ Historique granularité par action
- ✅ Layer-naming auto-détection + UI mapping (UC1)
- ✅ V0 one-shot, V1 step-by-step
- ✅ Warn + defaults pour données manquantes
- ✅ KG logiciel : scaffold-ready V0, meta-tools V1+
- ✅ KG user repoussé V2+
- ✅ Stratégie tokens : prompt caching + diff context + routing tier-1/2 + Haiku triage + trim history

À valider pour UC8 (compliance) :
- ⏳ Format corpus : Markdown + front-matter YAML
- ⏳ Localisation : `~/.config/claude-in-revit/compliance/` + companion projet
- ⏳ V1 full-text + cache, V2 RAG (seuil ~30 K tokens à confirmer)
- ✅ Audit hybride : primitives Python déterministes (familles principales) + fallback LLM (longue traîne), résultat marqué par `method_id` versionné
- ✅ `ProjectContext` (singleton KG) en V1 + `Compartment.affectation` (V2) + `Room.use_subcategory` (V1 optionnel) : modèle hiérarchique du contexte projet, provenance par champ
- ✅ Interview pré-audit (`gather_context`) : les primitives déclarent `requires_context`, l'audit collecte ce qui manque avant de tourner, persiste dans le KG → pas de re-questionnement
- ⏳ Liste exacte des primitives V1 (6 proposées : hauteur sous plafond, éclairement, largeurs de passage, allège, PMR porte, surfaces habitables)
- ✅ Famille Protection Incendie en V2 — référentiel pilote **AEAI** (Norme de protection incendie + DPI), citation format `aeai-2015#dpi-XX-XX-§Y`
- ✅ Primitives PI à seuils paramétrés (seuils dans le corpus, pas dans le code) → portage ERP/EN ultérieur sans toucher au Python
- ⏳ Extensions schéma KG (`fire_rating`, `is_exit`, `is_smokeproof`, `has_self_closer`, `is_smoke_vent`, `openable_area_m2`, node `Compartment`, edges associés)
- ⏳ Liste exacte des primitives PI V2 (6 proposées : fuite, typologie portes, largeur d'issue, distance entre issues, compartimentage, désenfumage) — confirmer la couverture vs DPI prioritaires AEAI
- ⏳ Format de violation et persistence (edge `violates` dans KG ou rapport externe)
- ⏳ Stratégie multi-juridictions et sortie du rapport (UI / PDF / annot Revit)

---

*Ce document est vivant. À mettre à jour lors du scaffolding et au fil des
itérations.*
