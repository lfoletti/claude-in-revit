# claude-planmaker

Agent LLM intégré à Autodesk Revit via PyRevit. L'utilisateur prompte en langage naturel ; le LLM orchestre des tools Revit via tool use (Anthropic Claude API), maintient un Knowledge Graph projet en NetworkX, et produit/modifie le modèle BIM.

Huit cas d'usage unifiés derrière un seul point d'entrée conversationnel (`prompt.pushbutton`), dont un audit de conformité réglementaire (UC8) hybride : primitives Python déterministes + corpus Markdown + fallback LLM.

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
