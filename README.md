# claude-in-revit

`claude-in-revit` est une extension PyRevit pensée moins comme un tool conversationnel sur l'API Revit que comme un point de rencontre entre le modèle BIM et des corpus externes — réglementations, typologies architecturales, références projet — compilés sous forme de graphes. L'agent les traverse pour produire des réponses qu'aucune macro ne sortirait : audits de conformité circonstanciés avec citations, croisements multi-corpus, programmes argumentés contre référentiel. Le LLM y agit moins en exécuteur qu'en *liant* entre BIM, règle métier et corpus de référence — les capacités d'orchestration des modèles récents (chaînage d'outils, raisonnement sur corpus longs) rendent possible cette approche aujourd'hui.

Trois Knowledge Graphs s'intercalent entre l'agent et le modèle Revit. Le **KG projet** maintient l'état BIM courant en NetworkX, synchronisé atomiquement à chaque mutation (transaction Revit + mutation graphe en tout-ou-rien — aucune divergence silencieuse tolérée). Le **KG logiciel** introspecte le catalogue d'outils lui-même et route les prompts vers le bon sous-ensemble (tier-2 conditionnel), pour ne pas saturer le contexte quand le catalogue dépasse ~80 tools. Le **KG corpus** indexe les référentiels Markdown : seuils et règles vivent dans le corpus, pas dans le code — d'où la portabilité multi-juridictions et la traçabilité de chaque citation dans les rapports. Ces trois couches alimentent un *diff context* envoyé au LLM (changements depuis le tour N-2), combiné au prompt caching Anthropic.

## Architecture

Une seule entrée conversationnelle (`prompt.pushbutton`) — pas de dispatch UI par cas d'usage, toute la richesse est portée par le catalogue de tools et le routing.

```
claude-in-revit.extension/
├── claude-in-revit.tab/planmaker.panel/
│   ├── prompt.pushbutton/         # unique entrée conversationnelle
│   ├── globals.pushbutton/        # variables projet (affectation, juridiction…)
│   └── refresh_kg.pushbutton/     # re-sync forcée KG ↔ Revit
├── lib/
│   ├── llm_api.py                 # HTTP Anthropic, caching, streaming, retry
│   ├── llm_protocol.py            # registry tools, schémas JSON, dispatch
│   ├── project_kg.py              # KG projet (NetworkX, persistence JSON)
│   ├── kg_sync.py                 # full re-scan Revit → KG
│   ├── tool_kg.py                 # KG logiciel (introspection docstrings)
│   ├── compliance_kb.py           # corpus réglementaire, index, citations
│   ├── compliance_investigations/ # primitives déterministes UC8
│   ├── revit_primitives.py        # wrappers Revit transactionnels
│   ├── routing.py context.py config.py
│   └── tools/                     # ~10 modules, ~50 tools en V0
│       ├── input.py walls.py openings.py rooms.py levels.py
│       ├── transforms.py query.py catalog.py bulk.py aggregations.py
│       └── compliance.py
└── LLM.md                          # prompt système versionné
```

Pipeline d'un tour : `routing.py` analyse le prompt et active les groupes tier-2 nécessaires ; `context.py` compose le payload (KG diff depuis le tour N-2, catalogue filtré, tier-1 toujours chargé) ; `llm_api.py` appelle Anthropic en multi-turn tool use ; le dispatcher exécute les `tool_use` reçus en boucle jusqu'à `stop_reason="end_turn"`. Toute écriture passe par un décorateur `@kg_synced` qui ouvre une transaction Revit, mute Revit puis le KG, et rollback les deux si l'un échoue — aucune divergence silencieuse tolérée.

## Stack

- **PyRevit** (CPython3) — extension hébergeant les pushbuttons
- **Anthropic Claude API** (Sonnet 4.6 par défaut, Haiku 4.5 pour triage)
- **NetworkX** pour le Knowledge Graph projet
- **ezdxf** pour parser DWG/DXF

## État

Pré-implémentation. Le repo contient :

- [`revit-planmaker-design.md`](revit-planmaker-design.md) — document de conception vivant (~940 lignes), source de vérité
- [`CLAUDE.md`](CLAUDE.md) — instructions de travail pour Claude Code
- [`JOURNAL.md`](JOURNAL.md) — journal de développement daté (décisions, phases, bugs)
- [`REVIT_API_NOTES.md`](REVIT_API_NOTES.md) — notes sur l'API Revit
- `lib/`, `scripts/`, `tests/` — premier scaffold (slice CLI hors-Revit)

Le runtime de production cible PyRevit dans Revit ; la `.venv` locale sert pour l'outillage (linting, tests des primitives compliance, parsing DWG).

## Licence

À définir.
