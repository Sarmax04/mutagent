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
    trimmed = data.get("trimmed") or {}
    if trimmed.get("after") is not None and trimmed.get("after") < trimmed.get("before", 0):
        lines.append(f"- New tests: {trimmed['after']} (trimmed from {trimmed['before']}; the others caught nothing extra)")
    c0, c1 = data.get("initial_coverage"), data.get("final_coverage")
    if isinstance(c0, (int, float)) and isinstance(c1, (int, float)):
        lines.append(
            f"- Line coverage: {c0:.1f}% -> {c1:.1f}% "
            "(lines the tests run; the score above says whether they would notice a bug there)"
        )
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
    lookups = data.get("docs_lookups") or []
    if lookups:
        lines += ["", "## Documentation the agent read (Tavily)", ""]
        for item in lookups:
            urls = ", ".join(u for u in item.get("urls", []) if u) or "no results"
            lines.append(f"- `{item.get('name', '?')}`: {urls}")
    attempts = data.get("attempts") or []
    if attempts:
        lines += ["", "## Attempts (best of N)", "", "| Attempt | Final score | Tokens | Kept |", "|---|---|---|---|"]
        for item in attempts:
            kept = "yes" if item.get("kept") else ""
            lines.append(f"| {item['attempt']} | {item['final_score']:.1f}% | {item['tokens_used']} | {kept} |")
    code = data.get("generated_code", "").strip()
    if code:
        lines += ["", "## Generated tests", "", "```python", code, "```"]
    return "\n".join(lines) + "\n"
