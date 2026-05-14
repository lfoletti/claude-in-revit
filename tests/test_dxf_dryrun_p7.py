"""Golden test for the DXF dryrun on the P7 fixture.

Runs `scripts.dxf_dryrun.main()` against `tests/fixtures/P7/` and asserts
the resulting JSON matches `tests/fixtures/P7/golden.json` byte for byte
after path normalisation.

When the pipeline intentionally changes output (algo tweak, new field,
etc.), regenerate the golden::

    python -m scripts.dxf_dryrun
    cp tests/fixtures/P7/dryrun_output.json tests/fixtures/P7/golden.json

Inspect the diff first — a churning golden is a churning algo.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import dxf_dryrun

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "P7"


@pytest.fixture
def dryrun_output(tmp_path: Path) -> dict:
    out_path = tmp_path / "dryrun_output.json"
    rc = dxf_dryrun.main([
        "--project-dir", str(_FIXTURE_DIR),
        "--out", str(out_path),
    ])
    assert rc == 0, "Dryrun exited non-zero"
    assert out_path.exists(), "Dryrun did not produce an output file"
    return json.loads(out_path.read_text(encoding="utf-8"))


@pytest.fixture
def golden() -> dict:
    return json.loads((_FIXTURE_DIR / "golden.json").read_text(encoding="utf-8"))


def test_p7_dryrun_matches_golden(dryrun_output, golden):
    """Full deep-equality vs the committed golden.

    `project_dir` is normalised to `<P>` by the driver, so the comparison
    holds across machines. Any mismatch is a real algorithmic shift.
    """
    if dryrun_output != golden:
        # Surface the *first* diverging path through the structures —
        # the raw dict comparison output is unreadable for 76 KB JSON.
        _diff_dump(dryrun_output, golden)
        pytest.fail(
            "Dryrun JSON differs from golden — see diff above. "
            "If the change is intentional, run "
            "`cp tests/fixtures/P7/dryrun_output.json tests/fixtures/P7/golden.json` "
            "after inspecting the diff."
        )


def test_p7_dryrun_key_counts(dryrun_output):
    """Smoke assertions on the headline counts.

    These guard against silent regressions where the golden gets stale
    but still matches itself byte-for-byte (e.g. somebody regenerated
    the golden after introducing a bug).
    """
    kg = dryrun_output["kg_final"]
    assert len(kg["Level"]) == 3, "Expected 3 levels (N0/N1/N2)"
    assert len(kg["Wall"]) == 19, "Expected 19 walls (9 N0 + 10 N1)"
    assert len(kg["Window"]) == 13, "Expected 13 windows (15 plan - 2 oversize)"
    assert len(kg["Floor"]) == 2, "Expected 2 floors (N0 + N1, N2 skipped as roof)"

    ao = dryrun_output["add_openings_to_walls_many"]
    assert ao["plan_openings_detected"] == 15
    assert ao["openings_oversize_for_wall"] == 2


def _diff_dump(actual, expected, path: str = "") -> bool:
    """Print first 10 diverging paths through nested dicts/lists.

    Returns True iff any diff was printed (i.e. structures differ).
    Caps output so pytest's failure window stays readable.
    """
    found = [False]
    count = [0]
    cap = 10

    def walk(a, b, p):
        if count[0] >= cap:
            return
        if type(a) is not type(b):
            print("  diff at {}: type {} vs {}".format(p, type(a).__name__, type(b).__name__))
            count[0] += 1
            found[0] = True
            return
        if isinstance(a, dict):
            for k in sorted(set(a) | set(b)):
                if k not in a:
                    print("  diff at {}.{}: missing in actual".format(p, k))
                    count[0] += 1
                    found[0] = True
                elif k not in b:
                    print("  diff at {}.{}: missing in expected".format(p, k))
                    count[0] += 1
                    found[0] = True
                else:
                    walk(a[k], b[k], "{}.{}".format(p, k))
        elif isinstance(a, list):
            if len(a) != len(b):
                print("  diff at {}: length {} vs {}".format(p, len(a), len(b)))
                count[0] += 1
                found[0] = True
            else:
                for i, (x, y) in enumerate(zip(a, b)):
                    walk(x, y, "{}[{}]".format(p, i))
        else:
            if a != b:
                print("  diff at {}: {!r} vs {!r}".format(p, a, b))
                count[0] += 1
                found[0] = True

    walk(actual, expected, path or "$")
    if count[0] >= cap:
        print("  (truncated at {} diffs)".format(cap))
    return found[0]
