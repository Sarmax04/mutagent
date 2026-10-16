"""Turn a saved run report (JSON) into a readable Markdown report."""

from __future__ import annotations


def render_markdown(data: dict) -> str:
    mode = data.get("mode", "guided")
    title = "Baseline run" if mode == "baseline" else "mutagent run"
    lines = [
        f"# {title}: `{data['target']}`",
        "",
        f"- Mutants: {data['mutant_count']}",
        f"- Score: **{data['initial_score']:.1f}% -> {data['final_score']:.1f}%**",
        f"- Tokens used: {data['tokens_used']}",
    ]
    models = data.get("models")
    if models:
        lines.append(f"- Models: strong `{models.get('strong')}`, fast `{models.get('fast')}`")
    lines += ["", "## Rounds", "", "| Round | Score | Killed | Survived | Result | Tokens |", "|---|---|---|---|---|---|"]
    for r in data.get("rounds", []):
        lines.append(
            f"| {r['round']} | {r['score']:.1f}% | {r['killed']} | {r['survived']} | {r['note']} | {r['tokens_used']} |"
        )
    rounds = data.get("rounds", [])
    alive = set(rounds[-1].get("survivors", [])) if rounds else set()
    by_id = {m["id"]: m for m in data.get("mutants", [])}
    lines += ["", "## Bugs still alive", ""]
    if alive:
        why = data.get("explanations", {})
        lines += ["| Mutant | Line | Kind | Why it survived |", "|---|---|---|---|"]
        for i in sorted(alive):
            m = by_id.get(i, {})
            note = why.get(str(i), "").replace("|", "/")
            lines.append(f"| #{i} | {m.get('line', '?')} | {m.get('kind', '?')} | {note} |")
        lines += ["", "Some of these may be equivalent mutants: changes that no test can ever tell apart."]
    else:
        lines.append("None. Every planted bug was caught.")
    code = data.get("generated_code", "").strip()
    if code:
        lines += ["", "## Generated tests", "", "```python", code, "```"]
    return "\n".join(lines) + "\n"
