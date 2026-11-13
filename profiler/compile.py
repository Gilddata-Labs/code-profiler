"""GildData side: merge catalogue zips from several suppliers into one catalogue,
find commits shared across suppliers, write the buyer workbooks and GildScore.
Needs openpyxl (pip install openpyxl)."""
from __future__ import annotations

import csv
import json
import tempfile
import zipfile
from pathlib import Path

from . import catalogue as cat
from .extract import mark_duplicates, raw_to_score_row
from .scorecard import PILLARS, render_html, score
from .util import log, safe_name, stamp

SCORE_COLUMNS = ["GildScore", "Grade", *[PILLARS[p][0] for p in PILLARS], "Score limited by", "Watch outs"]


def _open(inp: Path, tmp: Path) -> list[Path]:
    if inp.is_file() and inp.suffix == ".zip":
        dest = tmp / safe_name(inp.stem)
        with zipfile.ZipFile(inp) as z:
            for m in z.namelist():
                if m.startswith("/") or ".." in Path(m).parts:
                    raise ValueError(f"unsafe path in {inp}: {m}")
            z.extractall(dest)
        return sorted(p.parent for p in dest.rglob("raw_metrics.csv"))
    return sorted(p.parent for p in inp.rglob("raw_metrics.csv")) if inp.is_dir() else []


def _read_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _num(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) else f
    except (TypeError, ValueError):
        return {"True": True, "False": False}.get(v, v)


def compile_runs(inputs: list[Path], out: Path, clients: list[str] | None = None) -> Path:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise SystemExit("compile needs openpyxl: pip install openpyxl")
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gdp-compile-"))
    cat_rows, raws, qc, missing, groups = [], [], [], [], []
    for inp in inputs:
        for job in _open(inp, tmp):
            src = job.name
            cat_rows += [dict(r, _source=src) for r in _read_csv(job / "summary.csv")]
            qc += [dict(r, run=src) for r in _read_csv(job / "qc" / "qc_report.csv")]
            missing += [dict(r, run=src) for r in _read_csv(job / "qc" / "missing_fields.csv")]
            groups += [dict(r, run=src) for r in _read_csv(job / "qc" / "duplicate_groups.csv")]
            for r in _read_csv(job / "raw_metrics.csv"):
                r = {k: _num(v) for k, v in r.items()}
                key = safe_name(r["full_name"])
                shas, loc = [], {}
                cf = job / "evidence" / key / "commits.jsonl"
                if cf.exists():
                    for line in cf.open(encoding="utf-8"):
                        c = json.loads(line)
                        loc[c["sha"]] = sum(f["additions"] + f["deletions"] for f in c.get("files", []))
                        if len(c.get("parents", [])) <= 1:
                            shas.append(c["sha"])
                ps = job / "evidence" / key / "pr_stats.json"
                sig = (json.loads(ps.read_text()).get("pr_signals") or []) if ps.exists() else []
                r.update({"_full_name": f"{src}:{r['full_name']}", "_commit_shas": shas, "_commit_loc": loc,
                          "_pr_commits": {str(s["number"]): s["commit"] for s in sig}, "_source": src})
                raws.append(r)
    if not raws:
        raise SystemExit("No catalogue output found in the inputs.")
    # commits shared across suppliers (forks or the same code sold twice)
    for r in raws:
        r["duplicate_pct"] = r["duplicate_of"] = ""
    mark_duplicates(raws)
    for r in raws:
        if r.get("duplicate_of") and r["duplicate_of"].split(":")[0] != r["_source"]:
            qc.append({"repo": r["full_name"], "flag": "DUPLICATE ACROSS SUPPLIERS",
                       "value": r["duplicate_pct"], "rule": f"commits also in {r['duplicate_of']}",
                       "run": r["_source"]})
    scored = {}
    for r in raws:
        sr = raw_to_score_row({**r, "duplicate_of": (r.get("duplicate_of") or "").split(":", 1)[-1]}, r["_source"])
        sr["_score"] = score(sr)
        sr["gildscore"], sr["grade"] = sr["_score"]["gildscore"], sr["_score"]["grade"]
        scored[(r["_source"], r["full_name"])] = sr
    for row in cat_rows:
        sr = scored.get((row["_source"], row.get("Repo Full Name")))
        if sr:
            s = sr["_score"]
            row.update({"GildScore": s["gildscore"], "Grade": s["grade"],
                        **{PILLARS[p][0]: s[f"{p}_score"] for p in PILLARS},
                        "Score limited by": s["capped_by"], "Watch outs": " | ".join(s["flags"])})

    columns, sources, _, _ = cat.template_spec()
    name = f"compiled-{stamp()}"
    cat.write_catalogue(cat_rows, out / f"{name}-catalogue.xlsx", columns, sources)
    with (out / f"{name}-summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SCORE_COLUMNS + columns + ["Source run"], extrasaction="ignore")
        w.writeheader()
        for r in cat_rows:
            w.writerow({**r, "Source run": r["_source"]})
    _scores_xlsx(cat_rows, out / f"{name}-gildscore.xlsx", columns)
    have_templates = cat.META_TEMPLATE.exists() and cat.HUD_TEMPLATE.exists()
    for c in (clients or ["meta", "hud"]) if have_templates else []:   # templates are not part of every build
        if c == "meta":
            cat.write_meta(cat_rows, out / f"{name}-meta_vendor_quote.xlsx")
        if c == "hud":
            cat.write_hud(cat_rows, out / f"{name}-hud_codebase_eval.xlsx")
    cat.write_csv(qc, out / f"{name}-qc_report.csv", ["run", "repo", "flag", "value", "rule"])
    cat.write_csv(missing, out / f"{name}-missing_fields.csv", ["run", "repo", "column", "who_fills"])
    cat.write_csv(groups, out / f"{name}-duplicate_groups.csv", ["run", "group_id", "method", "repos", "overlap"])
    cutoffs = {str(r.get("cutoff_date")) for r in raws if r.get("cutoff_date")}
    label = next(iter(cutoffs))[:4] if len(cutoffs) == 1 else "the cutoff"
    card = out / f"{name}-gildscore.html"
    card.write_text(render_html(list(scored.values()), label), encoding="utf-8")
    log.info("Compiled %d repos from %d inputs", len(cat_rows), len(inputs))
    return card


def _scores_xlsx(rows: list[dict], path: Path, columns: list[str]) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    fills = {"A": "C6EFCE", "B": "E2F0D9", "C": "FFF2CC", "D": "FCE4D6", "F": "F8CBAD"}
    wb = Workbook()
    ws = wb.active
    ws.title = "GildScore"
    head = SCORE_COLUMNS + ["Repo Full Name", "Primary Coding Language", "Logical LOC (excl. blanks & comments)",
                            "Avg LoC per PR", "Duplication Ratio (0.00-1.00)", "Cross-Repo Duplication Ratio",
                            "# of Contributors", "Source run"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in sorted(rows, key=lambda x: -(float(x.get("GildScore") or 0))):
        ws.append([_num(r.get(c, "")) if c != "Source run" else r["_source"] for c in head])
        g = r.get("Grade")
        if g in fills:
            ws.cell(ws.max_row, 2).fill = PatternFill("solid", fgColor=fills[g])
    ws.freeze_panes = "C2"
    wb.save(path)


# --------------------------------------------------------------- compare
V1_TO_V2 = {   # v1 summary.csv names -> catalogue names
    "loc": "Logical LOC (excl. blanks & comments)", "total_files": "Total Files",
    "size_kb": "Size (KB)", "primary_language": "Primary Coding Language",
    "languages_breakdown": "Language Breakdown (% of corpus)", "default_branch": "Default Branch",
    "total_commits": "# of Commits (total)", "first_commit": "First Commit Date",
    "last_commit_date": "Last Commit Date", "span_days": "Active Span (days)",
    "recency_days": "Recency (days)", "human_authors": "# of Contributors",
    "bot_authors": "Bot Authors", "bot_commit_ratio": "Bot Commit Ratio",
    "has_tests": "Has Tests", "has_test_runner": "Has Test Runner", "has_ci_cd": "Has CI/CD",
    "has_library_code": "Has Library Code", "merged_prs": "# of Merged PRs",
    "function_count": "# Functions", "class_count": "# Classes / Types",
    "untested_files_pct": "% Untested Files (heuristic)", "codebase_description": "Codebase Description",
}


def _summary_rows(inp: Path, tmp: Path) -> dict[str, dict]:
    jobs = []
    if inp.is_file() and inp.suffix == ".zip":
        dest = tmp / safe_name(inp.stem)
        with zipfile.ZipFile(inp) as z:
            z.extractall(dest)
        jobs = [p.parent for p in dest.rglob("summary.csv")]
    elif inp.is_dir():
        jobs = [p.parent for p in inp.rglob("summary.csv")]
    elif inp.name == "summary.csv":
        jobs = [inp.parent]
    out = {}
    for job in jobs:
        for row in _read_csv(job / "summary.csv"):
            if "Repo Full Name" in row:   # catalogue format
                key = row["Repo Full Name"]
            else:                          # v1 format
                key = f"{row['org']}/{row['repo']}"
                row = {V1_TO_V2.get(k, k): v for k, v in row.items()}
                for k in ("Has Tests", "Has Test Runner", "Has CI/CD", "Has Library Code"):
                    if row.get(k) in ("True", "False"):
                        row[k] = "Yes" if row[k] == "True" else "No"
                if row.get("% Untested Files (heuristic)"):
                    try:
                        row["% Untested Files (heuristic)"] = str(float(row["% Untested Files (heuristic)"]) / 100)
                    except ValueError:
                        pass
            out[key.split("/", 1)[-1].lower()] = row
    return out


def compare_runs(reference: Path, ours: Path, out: Path | None = None) -> Path:
    """Side by side of two outputs (v1 or catalogue format), per repo and column.
    Repos are matched on the repo name, so local and platform runs line up."""
    tmp = Path(tempfile.mkdtemp(prefix="gdp-compare-"))
    ref, mine = _summary_rows(reference, tmp / "a"), _summary_rows(ours, tmp / "b")
    common = sorted(set(ref) & set(mine))
    if not common:
        raise SystemExit("No repo appears in both outputs.")
    columns, _, _, _ = cat.template_spec()
    lines = []
    for key in common:
        for col in columns:
            a, b = ref[key].get(col, ""), mine[key].get(col, "")
            if a in ("", None) and b in ("", None):
                continue
            try:
                fa, fb = float(str(a).split(",")[0]), float(str(b).split(",")[0])
                diff = round(fb - fa, 4)
                rel = f"{(fb - fa) / fa * 100:+.1f}%" if fa else ""
                status = "match" if abs(fb - fa) < 1e-9 else (
                    "within 1%" if fa and abs(fb - fa) / abs(fa) <= 0.01 else
                    "within 5%" if fa and abs(fb - fa) / abs(fa) <= 0.05 else "differs")
            except ValueError:
                diff, rel = "", ""
                status = "match" if str(a).strip().lower() == str(b).strip().lower() else (
                    "only in ours" if a in ("", None) else "only in reference" if b in ("", None) else "differs")
            lines.append({"repo": key, "column": col, "reference": a, "ours": b,
                          "difference": diff, "difference_pct": rel, "status": status})
    out = out or Path(f"compare-{stamp()}.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(lines[0]))
        w.writeheader()
        w.writerows(lines)
    from collections import Counter
    counts = Counter(x["status"] for x in lines)
    print(f"Compared {len(common)} repos: " + ", ".join(f"{v} {k}" for k, v in counts.most_common()))
    for x in lines:
        if x["status"] == "differs":
            print(f"  {x['repo']} | {x['column']}: reference {x['reference']} | ours {x['ours']}")
    return out
