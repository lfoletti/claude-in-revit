#! python3
# -*- coding: utf-8 -*-
"""pyRevit hook : `Application.DocumentChanged`.

Appended one line per non-LLM transaction commit to the per-project JSONL
buffer `~/.config/claude-in-revit/projects/<id>.pending_diffs.jsonl`.
Consumed paresseusement par `kg_sync.consume_pending_diffs(kg, doc)` au
début de chaque tour agent (cf. `prompt.pushbutton/script.py`).

**Discipline absolue** (cf. JOURNAL 2026-05-14 session v — hooks Revit) :
- Read-only côté Revit (pas de Tx ouverte ici — Revit lèverait
  `InvalidOperationException` sinon, issue pyRevit #1659).
- Cible **< 10 ms** au total : on tient sur le thread UI Revit, tout
  ralentissement ralentit chaque commit user.
- Pas d'appel à `Refresh KG`, pas de mutation KG, pas de file lock.
  Append-only JSONL, point.

Early-skip dans deux cas :
1. Sentinel `~/.config/claude-in-revit/hooks.disabled` présent — l'user
   a explicitement coupé l'auto-sync via le pushbutton `kg_autosync`.
2. **Toutes** les transactions du batch portent le préfixe `[LLM] ` —
   c'est notre agent qui mute, le KG est déjà à jour atomiquement.

Si CN écit en CPython lance une exception inattendue, on l'écrit dans
un fichier sentinel `~/.config/claude-in-revit/last_hook_error.txt`
pour debug post-mortem. JAMAIS de re-throw — un crash de hook gèlerait
les commits Revit.
"""
import os
import sys
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

# sys.path fixup — pyRevit met `<extension>/lib/` sur sys.path mais pas
# la racine de l'extension. Pattern identique aux pushbuttons.
_EXT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _EXT_ROOT not in sys.path:
    sys.path.insert(0, _EXT_ROOT)


def _write_error(exc: BaseException) -> None:
    """Trace l'erreur sans jamais re-lever ; lit par debug à froid."""
    try:
        from pathlib import Path as _P
        err_path = _P.home() / ".config" / "claude-in-revit" / "last_hook_error.txt"
        err_path.parent.mkdir(parents=True, exist_ok=True)
        err_path.write_text(
            "{}\n{}: {}\n{}".format(
                datetime.now(timezone.utc).isoformat(),
                type(exc).__name__, exc, traceback.format_exc(),
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        # Couche ultime : si même écrire un fichier d'erreur échoue,
        # on laisse passer silencieusement. Revit doit pouvoir commit.
        pass


def _handle() -> None:
    # Early-skip 1 : sentinel ON ?
    from lib import config
    if config.are_hooks_disabled():
        return

    # `__eventargs__` (DocumentChangedEventArgs) et `__eventsender__`
    # sont injectés par pyRevit dans le namespace du hook au moment
    # de l'event dispatch.
    try:
        args = __eventargs__  # type: ignore[name-defined]
    except NameError:
        # Pas de event_args dispo — appelé hors contexte hook (test
        # interactif depuis un script ?). Rien à faire.
        return

    # Filter `[LLM] *` transactions : nos propres mutations agent ont
    # déjà mis à jour le KG atomiquement via `@kg_synced`. Re-processer
    # ferait du double-work + casserait l'idempotence dans certains cas.
    tx_names = list(args.GetTransactionNames())
    from lib.revit_primitives import AGENT_TX_PREFIX
    non_agent_tx = [n for n in tx_names if not str(n).startswith(AGENT_TX_PREFIX)]
    if not non_agent_tx:
        return

    # Récupère les IDs (.Value sur ElementId en Revit 2024+, IntegerValue
    # avant — on tente .Value puis fallback).
    def _eid_to_int(eid):
        for attr in ("Value", "IntegerValue"):
            v = getattr(eid, attr, None)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return None

    added = [_eid_to_int(e) for e in args.GetAddedElementIds()]
    modified = [_eid_to_int(e) for e in args.GetModifiedElementIds()]
    deleted = [_eid_to_int(e) for e in args.GetDeletedElementIds()]
    added = [i for i in added if i is not None]
    modified = [i for i in modified if i is not None]
    deleted = [i for i in deleted if i is not None]

    # Rien d'utile à enregistrer.
    if not (added or modified or deleted):
        return

    # Résolution project_id : on prend le doc de l'event (sécurise les
    # cas multi-doc où l'utilisateur a 2 projets ouverts).
    from lib import kg_sync
    doc = args.GetDocument()
    project_id = kg_sync.project_id_for(doc)
    diffs_path = config.pending_diffs_path_for(project_id)
    diffs_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tx_names": non_agent_tx,
        "added": added,
        "modified": modified,
        "deleted": deleted,
    }
    # Append-only : JSONL = une ligne, O(1) (modulo open/close ~1-3 ms
    # sur Windows). On garde le mode "a" qui crée si absent.
    with diffs_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False))
        fh.write("\n")


try:
    _handle()
except BaseException as exc:  # noqa: BLE001 — Revit doit pouvoir commit.
    _write_error(exc)
