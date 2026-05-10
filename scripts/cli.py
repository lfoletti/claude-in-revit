"""scripts/cli.py — interactive harness for the slice.

Run from the repo root:
    python -m scripts.cli
or:
    python scripts/cli.py    # uses the path-fixup block below

Requires ANTHROPIC_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/cli.py` from repo root without `pip install -e .`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.llm_api import LLMClient  # noqa: E402
from lib.project_kg import ProjectKG  # noqa: E402


def bootstrap_kg(kg: ProjectKG) -> None:
    """Seed a fresh KG with two Levels and one WallType so walls_create can run."""
    if kg.find_by_type("Level") or kg.find_by_type("WallType"):
        return
    with kg.transaction():
        kg.add_node("Level", {"name": "N00", "elevation": 0.0})
        kg.add_node("Level", {"name": "N01", "elevation": 3.0})
        kg.add_node("WallType", {"name": "STD200", "total_thickness": 0.2})


def build_system_prompt(kg: ProjectKG) -> str:
    return (
        "You are a Revit planmaker assistant — slice prototype.\n\n"
        "You operate on a project's Knowledge Graph (KG) via tools. The KG\n"
        "mirrors what would normally be Revit elements but stays in memory +\n"
        "JSON for now (no Revit binding in this slice).\n\n"
        "Project ID: {project_id}\n"
        "Current turn: {turn}\n\n"
        "Conventions:\n"
        "- Coordinates are 2D (x, y) in metres on the level's plan.\n"
        "- Heights and thicknesses are in metres.\n"
        "- llm_ids are stable identifiers like 'level_001', 'wall_003' — pass\n"
        "  them as refs to other tools.\n"
        "- Before creating walls, call catalog_list_levels and\n"
        "  catalog_list_wall_types to discover available references.\n\n"
        "When done acting, respond conversationally summarising what you did,\n"
        "in the user's language."
    ).format(project_id=kg.project_id, turn=kg.turn)


def _fmt_usage(u: object) -> str:
    fields = ("api_calls", "input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")
    parts = []
    for f in fields:
        parts.append("{}={}".format(f, getattr(u, f, "?")))
    return " | ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", default="slice-demo")
    parser.add_argument(
        "--persist-path",
        default="scratch_kg/slice-demo.kg.json",
        help="Local JSON file for KG state. Created on first run.",
    )
    parser.add_argument("--model", default=None, help="Override LLM model id.")
    parser.add_argument("--effort", default=None, help="low | medium | high | max")
    parser.add_argument(
        "--thinking",
        default="disabled",
        choices=("disabled", "adaptive"),
        help="Adaptive thinking on/off (default off in slice for cost).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the persisted KG before starting.",
    )
    args = parser.parse_args()

    persist_path = Path(args.persist_path)
    if args.reset and persist_path.exists():
        persist_path.unlink()

    if persist_path.exists():
        kg = ProjectKG.load(persist_path)
        # `load` doesn't carry persist_path info forward — set it explicitly.
        kg.persist_path = persist_path
    else:
        kg = ProjectKG(project_id=args.project_id, persist_path=persist_path)
        bootstrap_kg(kg)

    client_kwargs = {"thinking": args.thinking}
    if args.model:
        client_kwargs["model"] = args.model
    if args.effort:
        client_kwargs["effort"] = args.effort
    client = LLMClient(**client_kwargs)

    history: list = []
    print("Loaded KG: {} (turn {})".format(kg.project_id, kg.turn))
    print(
        "Levels: {} | WallTypes: {} | Walls: {}".format(
            kg.count_by_type("Level"),
            kg.count_by_type("WallType"),
            kg.count_by_type("Wall"),
        )
    )
    print("Model: {} | effort: {} | thinking: {}".format(
        client.model, client.effort, client.thinking
    ))
    print("Persist: {}".format(persist_path))
    print("Type a prompt (Ctrl-D, 'quit' or 'exit' to leave):")
    print()

    while True:
        try:
            prompt = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not prompt:
            continue
        if prompt.lower() in ("quit", "exit", ":q"):
            break
        if prompt.lower() == ":kg":
            print("nodes: {} | edges: {} | turn: {}".format(
                kg._g.number_of_nodes(),  # noqa: SLF001
                kg._g.number_of_edges(),  # noqa: SLF001
                kg.turn,
            ))
            continue

        kg.advance_turn()
        try:
            result = client.run_turn(
                kg=kg,
                user_prompt=prompt,
                system_prompt=build_system_prompt(kg),
                history=history,
                tier_max=1,
            )
        except Exception as e:  # noqa: BLE001
            print("[error] {}: {}".format(type(e).__name__, e))
            continue

        print()
        if result.text:
            print(result.text)
        if result.tool_calls:
            print("\n[tools used: {}]".format(
                ", ".join(t["name"] for t in result.tool_calls)
            ))
        print("[{} | stop={}]".format(_fmt_usage(result.usage), result.stop_reason))
        print()

    print("\nKG persisted to {}".format(persist_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
