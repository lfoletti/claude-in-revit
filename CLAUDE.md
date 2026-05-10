# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## État du repo

Pré-implémentation. Aucun code à ce jour — uniquement `DESIGN.md` (document de conception vivant, ~940 lignes) et un venv Python 3.13. Le scaffolding suivra la structure décrite ci-dessous ; toute première contribution doit s'aligner sur le design doc avant d'inventer du code.

## Vision du produit

Agent LLM intégré à Autodesk Revit via PyRevit. L'utilisateur prompte en langage naturel ; le LLM orchestre des tools Revit via tool use (Anthropic Claude API), maintient un Knowledge Graph projet, et produit/modifie le modèle BIM. Un seul point d'entrée conversationnel (`prompt.pushbutton`), pas de dispatch UI par cas d'usage.

Huit use cases unifiés derrière ce même socle, dont UC8 (audit de conformité réglementaire) qui combine corpus Markdown externes + primitives Python déterministes + fallback LLM.

## Stack cible

- **PyRevit** (CPython3, pas IronPython) — extension hébergeant les pushbuttons.
- **Anthropic Claude API** via tool use ; Sonnet 4.6 par défaut, Haiku 4.5 pour triage des opérations triviales.
- **NetworkX** pour les Knowledge Graphs en mémoire.
- **ezdxf** pour parser DWG/DXF.
- Python 3.13 dans `.venv/` (déjà provisionné).

## Arborescence cible (extension PyRevit)

```
claude-in-revit.extension/
├── claude-in-revit.tab/agent.panel/
│   ├── prompt.pushbutton/script.py        # UNIQUE entrée conversationnelle
│   ├── globals.pushbutton/script.py
│   └── refresh_kg.pushbutton/script.py
├── lib/
│   ├── llm_api.py                  # HTTP, caching, streaming, retry
│   ├── llm_protocol.py             # registry tools, schema JSON, dispatch
│   ├── project_kg.py               # NetworkX wrapper, schéma, persistence
│   ├── kg_sync.py                  # full re-scan Revit → KG
│   ├── tool_kg.py                  # KG logiciel (scaffold V0, activation V1+)
│   ├── dwg_reader.py / dwg_classifier.py
│   ├── compliance_kb.py            # corpus réglementaire, index, citations
│   ├── compliance_investigations/  # primitives déterministes UC8
│   ├── revit_primitives.py
│   ├── context.py / routing.py / config.py
│   └── tools/                      # ~10 fichiers, ~50 tools en V0
│       ├── input.py walls.py openings.py rooms.py levels.py
│       ├── transforms.py query.py catalog.py bulk.py aggregations.py
│       └── compliance.py
├── LLM.md / extension.json / README.md
```

Pour le détail des sous-modules et de leurs responsabilités, voir `DESIGN.md` §3.

## Concepts d'architecture à respecter

Ces points sont des décisions formellement validées (§12 du design doc). Ne pas les remettre en cause sans raison explicite.

### 1. KG projet first-class dès V0
- Maintenu en parallèle du modèle Revit, persisté en JSON dans `~/.config/claude-in-revit/projects/<uuid>.kg.json` (+ companion file optionnel à côté du `.rvt`).
- **Idempotence/atomicité** : toute écriture passe par un décorateur `@kg_synced` qui ouvre la transaction Revit, mute Revit, mute KG, commit ; si l'un échoue, on rollback l'autre. Aucune divergence silencieuse tolérée.
- Soft delete par défaut (flag `deleted_at_turn=N`), purge auto à 50 tours.
- Historique granularité **par action**, pas par tour.
- Drift utilisateur (édition hors pipeline) → détection par comparaison attrs avant les queries lourdes + tool `kg.refresh()` / bouton `refresh_kg`.

### 2. Pipeline d'un tour (§6 du design doc)
```
routing.py (analyse prompt → groupes tier-2)
  → context.py (KG diff_since + catalogue filtré + tier-1/tier-2)
  → llm_api.py (Anthropic call, multi-turn tool use)
  → dispatcher (exécute tool_use, met à jour KG+Revit atomiquement)
  → boucle jusqu'à stop_reason=="end_turn"
  → KG.persist + log + message utilisateur
```

### 3. Tier-1 / Tier-2 + KG logiciel
- Tier-1 (~15 tools) toujours chargés ; tier-2 chargés par routing keyword (`ROUTING_RULES` dans `routing.py`).
- KG logiciel (graphe des tools eux-mêmes) : **scaffolder dès V0** par introspection des docstrings ; meta-tools `find_tools` / `find_similar_tools` / `find_tool_chain` activés quand le catalogue dépasse ~80 tools (V1+).
- **Convention docstring obligatoire** pour tous les tools — sections `Concepts:`, `Phrases:`, `Similar:`. Voir l'exemple §4.2 du design doc.

### 4. Économie de tokens (§7)
- Jusqu'à 4 cache breakpoints Anthropic : LLM.md / catalogue / corpus réglementaire (si UC8) / début conversation. TTL 5 min.
- KG diff context (changements depuis tour N-2), pas l'état complet.
- Triage par modèle : prompts simples (`déplace`, `supprime`, `renomme`, `compte`) → Haiku 4.5 ; création générative → Sonnet 4.6.
- Trim history après 3 tours (résumé compact des plus anciens).

### 5. UC8 — Audit de conformité (architecture hybride)
- **Corpus** : fichiers Markdown + front-matter YAML (`id`, `title`, `jurisdiction`, `version`, `scope`, `source`). Stockage `~/.config/claude-in-revit/compliance/<corpus_id>/` + companion projet possible.
- **Hybride** : primitives Python déterministes (familles principales, `method_id` versionné `_vN`) + fallback LLM signalé `method: "llm_inferred_v1"` (moindre confiance). Les seuils viennent du corpus, pas du code (portabilité multi-juridictions).
- **`ProjectContext`** (singleton du KG, V1) + `Compartment.affectation` (V2) + `Room.use_subcategory` (V1, optionnel) : modèle de scope hiérarchique. Chaque champ a sa provenance (`source`, `confidence`, `set_at_turn`).
- Chaque primitive déclare `requires_context=[...]` ; `compliance.audit()` calcule l'union et appelle `gather_context()` (interview ciblée) pour combler les gaps avant de tourner.
- **`data_gaps` obligatoire** dans chaque primitive : ne jamais retourner silencieusement sur donnée manquante.
- Famille Protection Incendie en V2, référentiel pilote AEAI (citations `aeai-2015#dpi-XX-XX-§Y`), nécessitera des extensions de schéma KG (`fire_rating`, `is_exit`, node `Compartment`, etc.).

## Commandes

Aucun script de build / test / lint n'existe encore — à mettre en place pendant la Semaine 1 de V0 (cf. §9 du design doc). À activer la venv pour toute exécution Python locale :

```bash
source .venv/bin/activate
```

NB : le runtime de production est **PyRevit dans Revit** (CPython3 embarqué), pas cette venv. La venv sert pour l'outillage local (linting, tests unitaires des primitives compliance, parsing DWG, etc.). Les imports Revit (`Autodesk.Revit.DB`, etc.) ne fonctionneront pas hors PyRevit.

## Workflow attendu pendant V0

§9 du design doc fixe l'ordre :
1. **Semaine 1** — Foundation : scaffold extension, `llm_api.py`, `llm_protocol.py`, `project_kg.py` (NetworkX + JSON), `kg_sync.py`, tools tier-1 minimum. À ce stade UC4 (quantitatifs) tourne.
2. **Semaines 2-3** — Géométrie complète (walls, openings, rooms, levels, transforms). UC2/UC3 fonctionnels.
3. **Semaines 4-5** — I/O (DWG) et bulk. UC1/UC5/UC7 fonctionnels.

UC6 (vision) → V1. UC8 (compliance) → V1, requiert un KG projet stable.

## Conventions

- **Langue** : design doc et commentaires en français (le projet est franco-suisse). Identifiants en anglais.
- **Convention de nommage Revit** : Family Types préfixés `LLM_*` (ou `MUR_*`) pour faciliter le filtrage du catalogue. Mentionné comme risque/mitigation §10.
- **Citations réglementaires** : format `<corpus_id>#<ancre>` (ex : `rcv-2024#hauteur-sous-plafond`, `aeai-2015#dpi-15-15-§3.2`). Toujours inclure `version` du corpus dans le rapport.
- **Disclaimer audit UC8** : « assistance, pas validation réglementaire » — à inclure en tête de chaque rapport généré.

## Source de vérité

`DESIGN.md` est le document vivant. Quand une décision d'implémentation diverge du doc, mettre à jour le doc dans le même commit (cf. note finale du doc). §11 liste les décisions ouvertes à trancher pendant le scaffolding ; §12 liste les décisions verrouillées.

## Journal de développement

`JOURNAL.md` à la racine du repo. **À tenir à jour à chaque session de travail significative** : nouvelles entrées datées `YYYY-MM-DD`, format documenté en tête du fichier (Contexte & objectif → Décisions → Phases → Validation → Reste à faire). Cross-références au design doc en `§N.M`. Les bugs rencontrés sont consignés avec leur cause racine et leur fix, pas seulement « j'ai corrigé X » — c'est ce qui rend le journal utile aux sessions futures.
