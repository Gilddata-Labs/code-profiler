"""GildScore: a 0 to 100 code report card with a letter grade.

Five pillars, each the average of its metrics scored 0 to 100 between a
"poor" and a "strong" threshold. Missing metrics are skipped and the weights
of what remains are rescaled, so a repo is never punished for data the
platform could not give.
"""
from __future__ import annotations

import html
import math

PILLARS = {
    "scale": ("Size", 15),
    "team": ("Team", 15),
    "practice": ("Testing and process", 25),
    "quality": ("Code quality", 20),
    "originality": ("Originality", 25),
}

# metric: (pillar, plain label, poor, strong, kind)
# kind: lin | log | bool | band (band: (zero_low, ideal_low, ideal_high, zero_high))
LOC_PER_PR_BAND = (50, 300, 500, 2_000)
METRICS = {
    "loc":                    ("scale", "Lines of code", 1_000, 200_000, "log"),
    "total_commits":          ("scale", "Saved changes (commits)", 50, 5_000, "log"),
    "pr_count":               ("scale", "Merged pull requests", 10, 1_000, "log"),
    "human_authors":          ("team", "Developers", 1, 15, "lin"),
    "bus_factor":             ("team", "People who wrote half the code", 1, 4, "lin"),
    "span_days":              ("team", "Age of the project (days)", 90, 1_095, "lin"),
    "active_months":          ("team", "Months with activity", 3, 36, "lin"),
    "recency_days":           ("team", "Days since last update", 730, 30, "lin"),
    "has_tests":              ("practice", "Has tests", 0, 1, "bool"),
    "has_ci_cd":              ("practice", "Automatic build checks (CI)", 0, 1, "bool"),
    "has_test_runner":        ("practice", "Tests can be run automatically", 0, 1, "bool"),
    "test_source_loc_pct":    ("practice", "Test code vs main code %", 0, 60, "lin"),
    "untested_files_pct":     ("practice", "Files with no tests %", 80, 10, "lin"),
    "pr_review_pct":          ("practice", "Pull requests reviewed %", 0, 70, "lin"),
    "pr_linked_issue_pct":    ("practice", "Pull requests tied to a ticket %", 0, 50, "lin"),
    "loc_per_pr":             ("practice", "Average lines changed per pull request", LOC_PER_PR_BAND, None, "band"),
    "docstring_coverage_pct": ("quality", "Functions with documentation %", 0, 60, "lin"),
    "comment_docstring_ratio": ("quality", "Comments per line of code", 0.01, 0.15, "lin"),
    "median_pr_loc":          ("quality", "Typical pull request size (lines)", 2_000, 200, "lin"),
    "commits_per_pr":         ("quality", "Commits per pull request", 1, 3, "lin"),
    "duplication_ratio_jscpd": ("quality", "Copy pasted code inside the repo (jscpd)", 0.40, 0.05, "lin"),
    "parser_coverage":        ("quality", "Code the parser could read", 0.6, 0.95, "lin"),
    "duplicate_pct":          ("originality", "History shared with another repo %", 50, 0, "lin"),
    "cross_repo_duplication_ratio": ("originality", "Code also found in another repo", 0.8, 0.0, "lin"),
    "oss_pct":                ("originality", "Open source code inside %", 40, 0, "lin"),
    "ai_pr_pct":              ("originality", "Pull requests made with AI tools %", 60, 0, "lin"),
    "ai_score":               ("originality", "AI written code score (LLM)", 80, 10, "lin"),
    "largest_commit_loc_pct": ("originality", "Biggest single upload % of code", 80, 20, "lin"),
}

# Serious red flags cap the score whatever the rest says: (test, cap, reason)
CAPS = [
    (lambda v: (v.get("duplicate_pct") or 0) > 50 and v.get("is_copy"), 40,
     "most of its history matches an older repo, so this looks like the copy"),
    (lambda v: (v.get("duplicate_pct") or 0) > 25, 55, "over a quarter of its history appears in another repo"),
    (lambda v: (v.get("oss_pct") or 0) > 50, 40, "over half the code is borrowed open source"),
    (lambda v: (v.get("ai_score") or 0) >= 80 or (v.get("ai_pr_pct") or 0) >= 80, 55,
     "most of the code looks AI made"),
    (lambda v: (v.get("largest_commit_loc_pct") or 0) > 80, 70,
     "one single upload added most of the code"),
]

GRADES = [(85, "A"), (70, "B"), (55, "C"), (40, "D"), (0, "F")]


def _f(v):
    if v in ("", None):
        return None
    if isinstance(v, bool):
        return float(v)
    s = str(v).split(",")[0].strip()
    if s in ("True", "False"):
        return 1.0 if s == "True" else 0.0
    try:
        return float(s)
    except ValueError:
        return None


def _scale(v: float, poor, strong, kind: str) -> float:
    if kind == "band":
        z_lo, i_lo, i_hi, z_hi = poor
        if i_lo <= v <= i_hi:
            return 100.0
        t = (v - z_lo) / (i_lo - z_lo) if v < i_lo else (z_hi - v) / (z_hi - i_hi)
        return round(max(0.0, min(1.0, t)) * 100, 1)
    if kind == "bool":
        return 100.0 if v else 0.0
    if kind == "log":
        v, poor, strong = (math.log10(max(x, 1)) for x in (v, poor, strong))
    t = (v - poor) / (strong - poor)
    return round(max(0.0, min(1.0, t)) * 100, 1)


def inputs(row: dict) -> dict:
    """Pull the scored values out of a summary row."""
    vibe = str(row.get("vibe_code_signals") or "")
    ai_score = vibe.split(",")[0] if vibe and vibe[0] != "-" else ""
    prs = _f(row.get("merged_prs"))
    vals = {k: _f(row.get(k)) for k in METRICS}
    vals["pr_count"] = prs if prs is not None else _f(row.get("local_merges"))
    vals["oss_pct"] = _f(row.get("open_source_loc_pct"))
    vals["ai_score"] = _f(ai_score)
    vals["is_copy"] = bool(row.get("duplicate_of"))
    vals["secrets_open"] = row.get("secrets_result") == "Findings - open"
    vals["pii_open"] = row.get("pii_result") == "Findings - open"
    vals["non_merge_commits"] = _f(row.get("non_merge_commits"))
    vals["data_share"] = (_f(row.get("data_markup_loc")) or 0) / (_f(row.get("loc")) or 1) if row.get("loc") else None
    if not vals.get("pr_count"):
        for k in ("pr_review_pct", "pr_linked_issue_pct", "commits_per_pr", "loc_per_pr"):
            vals[k] = None
    if row.get("platform") == "local" or prs is None:
        vals["pr_review_pct"] = vals["pr_linked_issue_pct"] = None
    return vals


def score(row: dict) -> dict:
    vals = inputs(row)
    per_metric, pillars = {}, {}
    for key, (pillar, _label, poor, strong, kind) in METRICS.items():
        v = vals.get(key)
        if v is None:
            continue
        per_metric[key] = _scale(v, poor, strong, kind)
        pillars.setdefault(pillar, []).append(per_metric[key])
    pillar_scores = {p: round(sum(v) / len(v), 1) for p, v in pillars.items()}
    weight = sum(PILLARS[p][1] for p in pillar_scores)
    total = round(sum(PILLARS[p][1] * s for p, s in pillar_scores.items()) / weight, 1) if weight else 0
    raw, capped_by = total, ""
    for test, cap, reason in CAPS:
        if test(vals) and total > cap:
            total, capped_by = float(cap), f"Score limited to {cap} because {reason}"
    grade = next(g for cut, g in GRADES if total >= cut)
    return {"gildscore": total, "grade": grade, "raw_score": raw, "capped_by": capped_by,
            **{f"{p}_score": pillar_scores.get(p, "") for p in PILLARS},
            "_metrics": per_metric, "_values": vals, "flags": flags(row, vals)}


def flags(row: dict, vals: dict) -> list[str]:
    out = []
    if (vals.get("duplicate_pct") or 0) > 25:
        where = short_name(row.get("duplicate_of") or "") or "another repo"
        out.append(f"Copied: {vals['duplicate_pct']:.0f}% of its history also appears in {where}")
    if (vals.get("oss_pct") or 0) > 5:
        out.append(f"Open source inside: {vals['oss_pct']:.0f}% of the code is borrowed open source")
    if (vals.get("ai_pr_pct") or 0) > 20:
        out.append(f"AI tools: {vals['ai_pr_pct']:.0f}% of pull requests were made with AI tools")
    if (vals.get("ai_score") or 0) >= 60:
        out.append(f"AI written: the AI review scored it {vals['ai_score']:.0f} out of 100")
    if (vals.get("largest_commit_loc_pct") or 0) > 60:
        out.append(f"Bulk upload: one single upload added {vals['largest_commit_loc_pct']:.0f}% of the code")
    lpp = vals.get("loc_per_pr")
    lo, hi = LOC_PER_PR_BAND[1], LOC_PER_PR_BAND[2]
    if lpp is not None and vals.get("pr_count"):
        if lpp > hi * 2:
            out.append(f"Few pull requests: {lpp:,.0f} lines changed per pull request (healthy is {lo} to {hi})")
        elif lpp < lo / 2:
            out.append(f"Many tiny pull requests: {lpp:,.0f} lines changed per pull request (healthy is {lo} to {hi})")
    if (vals.get("bus_factor") or 99) <= 1 and (vals.get("human_authors") or 0) > 1:
        out.append("Key person risk: one developer wrote half or more of the code")
    if (vals.get("recency_days") or 0) > 365:
        out.append(f"Inactive: last update was {vals['recency_days']:.0f} days ago")
    if not vals.get("has_tests"):
        out.append("No tests found")
    if vals.get("secrets_open"):
        out.append("Secrets: keys or passwords found in the code (supplier must clean before delivery)")
    if vals.get("pii_open"):
        out.append("Personal data: emails, phone numbers or IDs found (supplier must clean)")
    nm = vals.get("non_merge_commits")
    if nm is not None and nm <= 5:
        out.append(f"Shallow history: only {nm:.0f} real commits (buyers price snapshots lower)")
    d = vals.get("duplication_ratio_jscpd")
    if d is not None and d == 0 and (vals.get("loc") or 0) > 0:
        out.append("Copy paste ratio is exactly 0, which buyers audit")
    if (vals.get("data_share") or 0) > 0.5:
        out.append(f"Data heavy: {vals['data_share']:.0%} of lines are data or markup, not code")
    if (vals.get("cross_repo_duplication_ratio") or 0) > 0.8:
        out.append(f"Same code as another repo: {vals['cross_repo_duplication_ratio']:.0%} of its code appears elsewhere")
    return out


def short_name(full: str) -> str:
    return full.rsplit("/", 1)[-1] if full else ""


def seller_of(row: dict) -> str:
    """'raw-extract-acme-20261112T...' -> 'acme'."""
    src = str(row.get("_source") or "")
    m = __import__("re").match(r"(?:raw-extract|catalogue)-(.+)-\d{8}T\d{6}Z$", src)
    return (m.group(1) if m else row.get("org") or "").replace("__", " ").replace("_", " ")


# --------------------------------------------------------------- HTML
def _fmt(v):
    if v in ("", None):
        return "n/a"
    try:
        f = float(v)
        return f"{f:,.0f}" if f.is_integer() or abs(f) >= 100 else f"{f:,.1f}"
    except (TypeError, ValueError):
        return html.escape(str(v))


DETAIL_ROWS = [
    ("Size", [("Lines of code", "loc"), ("Main language", "primary_language"),
              ("Languages used", "languages_breakdown"), ("Number of files", "total_files")]),
    ("When the code was written", [("Lines written before {cutoff}", "loc_pre_cutoff"),
                      ("Lines written after {cutoff}", "loc_post_cutoff"),
                      ("Share written before {cutoff} %", "pct_loc_pre_cutoff"),
                      ("Lines by year", "loc_by_year")]),
    ("Pull requests", [("Merged pull requests", "merged_prs"),
                       ("Average lines changed per pull request", "loc_per_pr"),
                       ("Typical pull request size (lines)", "median_pr_loc"),
                       ("Commits per pull request", "commits_per_pr"),
                       ("Pull requests reviewed %", "pr_review_pct"),
                       ("Pull requests tied to a ticket %", "pr_linked_issue_pct"),
                       ("Typical hours from open to merge", "median_hours_to_merge"),
                       ("Simple / standard / rich %", "_tiers")]),
    ("Team", [("Contributors on the platform", "contributor_count"), ("Developers", "human_authors"),
              ("Developers active in last 90 days", "active_authors_90d"),
              ("People who wrote half the code", "bus_factor"),
              ("Top developer's share of commits %", "top_author_commit_pct"),
              ("Share of commits by bots", "bot_commit_ratio")]),
    ("Activity", [("Commits", "total_commits"), ("First commit", "first_commit"),
                  ("Last commit", "last_commit_date"), ("Age of the project (days)", "span_days"),
                  ("Months with activity", "active_months"),
                  ("Commits per active month", "commits_per_active_month")]),
    ("Testing and quality", [("Has tests", "has_tests"), ("Tests can be run automatically", "has_test_runner"),
                     ("Automatic build checks (CI)", "has_ci_cd"),
                     ("Test code vs main code %", "test_source_loc_pct"),
                     ("Files with no tests %", "untested_files_pct"),
                     ("Functions", "function_count"), ("Classes", "class_count"),
                     ("Functions with documentation %", "docstring_coverage_pct"),
                     ("Comments per line of code", "comment_docstring_ratio")]),
    ("Buyer checks", [("Copy pasted code ratio (jscpd)", "duplication_ratio_jscpd"),
                     ("Real commits (no merges or reverts)", "non_merge_commits"),
                     ("Code the parser could read", "parser_coverage"),
                     ("Secrets scan", "secrets_result"), ("Personal data scan", "pii_result")]),
    ("Originality", [("History shared with another repo %", "duplicate_pct"),
                     ("Code also found in another repo", "cross_repo_duplication_ratio"),
                     ("Copied from", "_dup_of"),
                     ("Open source code inside % (and pull requests touching it)", "open_source_loc_pct"),
                     ("Bundled third party libraries %", "library_modules_loc_pct"),
                     ("Pull requests made with AI tools %", "ai_pr_pct"),
                     ("AI signals (score, pull requests, over half AI)", "vibe_code_signals"),
                     ("Biggest single upload % of code", "largest_commit_loc_pct")]),
    ("About the code (AI review)", [("What it does", "codebase_description"),
                     ("Industry", "industry_domain"), ("Type", "repo_type")]),
]

COLOURS = {"A": "#1a7f37", "B": "#4c9a2a", "C": "#b58900", "D": "#cb6a00", "F": "#c62828"}


def render_html(rows: list[dict], cutoff_label: str = "2024") -> str:
    cards = []
    order = sorted(rows, key=lambda r: -float(r.get("gildscore") or 0))
    for r in order:
        sc = r["_score"]
        bars = "".join(
            f'<div class="bar"><span>{PILLARS[p][0]} <em>{PILLARS[p][1]}%</em></span>'
            f'<div class="track"><div class="fill" style="width:{sc[p + "_score"] or 0}%"></div></div>'
            f'<b>{_fmt(sc[p + "_score"])}</b></div>' for p in PILLARS)
        flag_list = ([sc["capped_by"]] if sc["capped_by"] else []) + sc["flags"]
        fl = "".join(f"<li>{html.escape(x)}</li>" for x in flag_list) or "<li class='ok'>Nothing to worry about</li>"
        vint = r.get("pct_loc_pre_cutoff")
        chips = []
        if vint not in ("", None):
            chips.append(f'<div class="chip"><span>Written before {cutoff_label}</span>'
                         f'<b>{_fmt(vint)}%</b><em>{_fmt(r.get("loc_pre_cutoff"))} of '
                         f'{_fmt(r.get("code_loc"))} lines</em></div>')
        lpp = r.get("loc_per_pr")
        if lpp not in ("", None):
            lo, hi = LOC_PER_PR_BAND[1], LOC_PER_PR_BAND[2]
            v = float(lpp)
            state = ("ok", "Healthy") if lo <= v <= hi else (("warn", "Too low") if v < lo else ("warn", "Too high"))
            chips.append(f'<div class="chip {state[0]}"><span>Average lines changed per pull request</span>'
                         f'<b>{_fmt(lpp)}</b><em>{state[1]} (healthy is {lo} to {hi})</em></div>')
        vint_html = f'<div class="chips">{"".join(chips)}</div>' if chips else ""
        tiers = " / ".join(_fmt(r.get(k)) for k in ("pr_simple_pct", "pr_standard_pct", "pr_rich_pct"))
        sections = []
        for title, items in DETAIL_ROWS:
            trs = []
            for label, key in items:
                v = (tiers if key == "_tiers" else short_name(r.get("duplicate_of") or "")
                     if key == "_dup_of" else r.get(key, ""))
                if v in ("", None, "n/a / n/a / n/a"):
                    continue
                trs.append(f"<tr><td>{html.escape(label.format(cutoff=cutoff_label))}</td>"
                           f"<td>{_fmt(v) if key != '_tiers' else v}</td></tr>")
            if trs:
                sections.append(f"<h4>{title}</h4><table>{''.join(trs)}</table>")
        cards.append(f"""
<section class="card">
  <header>
    <div><h2>{html.escape(r.get('repo') or short_name(r.get('_full_name', '')))}</h2>
    <p class="sub">Seller: <b>{html.escape(seller_of(r))}</b> · {html.escape(str(r.get('primary_language') or ''))} ·
      {_fmt(r.get('loc'))} lines of code · {_fmt(r.get('human_authors'))} developer{'' if str(r.get('human_authors')) == '1' else 's'}</p></div>
    <div class="grade" style="background:{COLOURS[sc['grade']]}">{sc['grade']}<small>{_fmt(sc['gildscore'])}</small></div>
  </header>
  {vint_html}<div class="bars">{bars}</div>
  <h4>Watch outs</h4><ul class="flags">{fl}</ul>
  <details><summary>All details</summary>{''.join(sections)}</details>
</section>""")
    method = "".join(
        f"<tr><td>{PILLARS[p][0]}</td><td>{w}%</td><td>"
        + ", ".join(html.escape(m[1]) for m in METRICS.values() if m[0] == p) + "</td></tr>"
        for p, (_, w) in PILLARS.items())
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>GildScore report cards</title>
<style>
:root{{--ink:#1d1d1f;--mute:#6e6e73;--line:#e5e5ea;--bg:#f6f6f8}}
body{{font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink);background:var(--bg);margin:0;padding:24px}}
h1{{margin:0 0 4px}} .lead{{color:var(--mute);margin:0 0 20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px}}
.card{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px}}
header{{display:flex;justify-content:space-between;gap:12px}} h2{{margin:0;font-size:17px;word-break:break-all}}
.sub{{color:var(--mute);margin:4px 0 0;font-size:12px}}
.grade{{color:#fff;border-radius:10px;min-width:64px;text-align:center;font-size:30px;font-weight:700;padding:6px 8px;line-height:1}}
.grade small{{display:block;font-size:12px;font-weight:500;margin-top:4px}}
.bars{{margin:14px 0}} .bar{{display:grid;grid-template-columns:170px 1fr 40px;align-items:center;gap:8px;margin:5px 0}}
.bar em{{color:var(--mute);font-style:normal;font-size:11px}}
.track{{background:var(--line);border-radius:6px;height:8px}} .fill{{background:#3a6df0;height:8px;border-radius:6px}}
.bar b{{text-align:right}} h4{{margin:12px 0 6px;font-size:13px}}
.flags{{margin:0;padding-left:18px;color:#b3261e}} .flags .ok{{color:#1a7f37}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}} td{{padding:3px 0;border-bottom:1px solid var(--line);vertical-align:top}}
td:last-child{{text-align:right;color:var(--ink);max-width:220px;word-break:break-word}} td:first-child{{color:var(--mute)}}
summary{{cursor:pointer;color:#3a6df0;margin-top:8px}}
.chips{{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}}
.chip{{background:#f0f4ff;border-radius:8px;padding:8px 10px}} .chip span{{display:block;font-size:11.5px;color:var(--mute)}}
.chip b{{font-size:20px}} .chip em{{display:block;font-style:normal;font-size:11.5px;color:var(--mute)}}
.chip.ok{{background:#e8f5ec}} .chip.ok em{{color:#1a7f37}} .chip.warn{{background:#fff4e5}} .chip.warn em{{color:#b26a00}}
.method td:last-child{{text-align:left;max-width:none}} .method td:nth-child(2){{text-align:left;width:60px}}
.method{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin-top:20px}}
</style></head><body>
<h1>GildScore report cards</h1>
<p class="lead">{len(rows)} repositories, best first. Grades: A 85+, B 70+, C 55+, D 40+, F below 40.
Click "All details" on a card for every number.</p>
<div class="grid">{''.join(cards)}</div>
<div class="method"><h3>How the score works</h3>
<p>Each metric is scored 0 to 100 between a poor and a strong threshold. A pillar is the average of its metrics,
and the GildScore is the weighted average of the pillars. Missing data is skipped, not penalised.
Code written before vs after {cutoff_label} is shown for valuation but not scored. Average lines changed per pull request
scores full marks between 300 and 500 and less the further it is outside that range.
Serious red flags cap the score: over half the commits shared with an older repo (the copy) or over half the code
open source caps it at 40; over a quarter shared, or most code showing AI signals, caps it at 55; one commit adding over 80%
of the code caps it at 70.</p>
<table>{method}</table></div>
</body></html>"""
