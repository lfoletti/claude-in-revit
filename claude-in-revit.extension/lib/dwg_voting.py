"""dwg_voting.py — framework de vote multi-sources (V2 Phase 2.5).

User 2026-05-13 : « c'est une approche par vote ». Chaque source (plan,
coupe, élévation) émet un `Vote` sur une hypothèse partagée. La décision
finale agrège les votes par majorité ou seuil de confidence.

Hypothèses typiques votées :
- « Ce candidat est un vrai mur » (plan : paire détectée ; coupe : présence
  verticale ; élévation : silhouette visible).
- « Ce mur est continu » (plan : INSERT A-GLAZ + width gap ; coupe :
  matching block_id ; élévation : linteau/allège visible).
- « Cette opening existe » (coupe : INSERT A-GLAZ avec sill/height ;
  élévation : présence de bande horizontale dans la zone).

Module pur — pas d'I/O fichier, pas d'import Revit. Réutilisé par
`dwg_plan_openings`, `dwg_elevation_reader`, et le tool d'import.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class Vote:
    """Un vote sur une hypothèse partagée.

    Champs :
    - `answer` : `True` (oui), `False` (non), ou `None` (abstention — la
      source n'a pas l'information).
    - `confidence` : 0.0–1.0 indiquant la fiabilité du vote. Une source
      qui « voit » clairement vote avec confidence ≈ 1 ; une source
      qui infère à partir d'un signal faible vote avec confidence < 0.5.
    - `source` : étiquette descriptive (`"plan"`, `"coupe"`, `"elevation_Nord"`,
      etc.) pour traçabilité.
    - `evidence` : payload optionnel décrivant ce qui a déclenché le vote
      (lignes A-WALL trouvées, distance perpendiculaire, etc.). Utile au
      debug + à présenter à l'user.
    """
    answer: Optional[bool]
    confidence: float
    source: str
    evidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Decision:
    """Résultat d'une agrégation de votes.

    Champs :
    - `answer` : décision finale (`True` / `False` / `None` si pas
      d'information suffisante).
    - `confidence_score` : 0.0–1.0 indiquant la robustesse de la décision.
    - `tally` : `{"yes": float, "no": float, "abstain": int}` somme des
      confidences par catégorie.
    - `votes` : liste complète des votes pour traçabilité.
    """
    answer: Optional[bool]
    confidence_score: float
    tally: Dict[str, float]
    votes: List[Vote]


def aggregate_votes(
    votes: Sequence[Vote],
    *,
    min_voters: int = 1,
    threshold: float = 0.5,
) -> Decision:
    """Agrège une séquence de `Vote` en une `Decision`.

    Stratégie : somme des `confidence` par answer (oui/non). La majorité
    pondérée gagne si elle dépasse `threshold` × total des votes
    non-abstention. Si total < `min_voters`, retourne `answer=None`
    (information insuffisante).

    Args:
        votes: la séquence de votes à agréger.
        min_voters: nombre minimum de votes non-abstention requis pour
            une décision. Défaut 1 (toute information suffit).
        threshold: ratio yes / (yes + no) au-dessus duquel on déclare
            « yes ». Défaut 0.5 (majorité simple). Mettre 0.66 pour
            exiger 2/3, etc.

    Returns:
        `Decision` avec answer, confidence_score, tally, votes.
    """
    yes_sum = 0.0
    no_sum = 0.0
    abstain_count = 0
    informative_count = 0
    for v in votes:
        if v.answer is None:
            abstain_count += 1
            continue
        informative_count += 1
        if v.answer:
            yes_sum += v.confidence
        else:
            no_sum += v.confidence

    tally = {"yes": yes_sum, "no": no_sum, "abstain": abstain_count}

    if informative_count < min_voters:
        return Decision(
            answer=None, confidence_score=0.0, tally=tally, votes=list(votes),
        )

    total = yes_sum + no_sum
    if total < 1e-9:
        return Decision(
            answer=None, confidence_score=0.0, tally=tally, votes=list(votes),
        )
    yes_ratio = yes_sum / total
    if yes_ratio >= threshold:
        answer = True
        confidence_score = yes_ratio
    else:
        answer = False
        confidence_score = 1.0 - yes_ratio
    return Decision(
        answer=answer,
        confidence_score=round(confidence_score, 4),
        tally={k: round(v, 4) if isinstance(v, float) else v for k, v in tally.items()},
        votes=list(votes),
    )


def yes_vote(source: str, confidence: float = 1.0, **evidence: Any) -> Vote:
    """Helper : vote `yes` avec confidence."""
    return Vote(answer=True, confidence=confidence, source=source, evidence=dict(evidence))


def no_vote(source: str, confidence: float = 1.0, **evidence: Any) -> Vote:
    """Helper : vote `no` avec confidence."""
    return Vote(answer=False, confidence=confidence, source=source, evidence=dict(evidence))


def abstain(source: str, **evidence: Any) -> Vote:
    """Helper : abstention (la source n'a pas l'info pour voter)."""
    return Vote(answer=None, confidence=0.0, source=source, evidence=dict(evidence))
