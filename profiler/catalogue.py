"""The 95 column catalogue: build rows from per repo facts, merge supplier answers,
roll up bundles, apply QC flags, and write catalogue.xlsx, summary.csv, the buyer
client workbooks and the qc/ files. Column names, sources and dropdowns
are read from the shipped template, whose Field Guide is the final word."""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .util import log, parse_date

TEMPLATES = Path(__file__).resolve().parent / "templates"
CATALOGUE_TEMPLATE = TEMPLATES / "catalogue_template.xlsx"
META_TEMPLATE = TEMPLATES / "client" / "meta_template.xlsx"
HUD_TEMPLATE = TEMPLATES / "client" / "hud_template.xlsx"
HEADER_ROW, SOURCE_ROW, FIRST_DATA_ROW = 4, 6, 9   # row 8 is the template's sample row

PLATFORM = {"github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket",
            "local": "Self-hosted / local", "azure": "Azure DevOps"}
COMPANY_COLUMNS = ["Originating Company (legal name)", "Sub-Company / Project Name",
                   "Originating Company's Industry", "Description of Business",
                   "Established Year", "Closed Year", "# of Employees", "# of Engineers",
                   "Quality of SWE", "Business Systems Covered", "References",
                   "Sample Code Available", "Authorship Type", "IP Ownership Confirmed",
                   "Private Code Attestation", "Git Delivery Format"]
SUM_COLS = ["# Repos in Dataset", "Total Files", "Source Files", "Test Files", "Size (KB)",
            "Physical LOC (incl. blanks & comments)", "Logical LOC (excl. blanks & comments)",
            "Data / Markup LOC", "Non-Authored LOC (auto-gen + vendored + deps)",
            "Source LOC (production)", "Test LOC", "# Functions", "# Classes / Types",
            "# Branches", "# Tags", "# of Commits (total)", "Non-Merge Commit Count",
            "# of PRs (total)", "# of Merged PRs", "# of Reviewed PRs", "Bot Authors"]


# ------------------------------------------------------------------ spec
def template_spec() -> tuple[list[str], dict[str, str], dict[str, list[str]], dict[str, str]]:
    """(columns, source by column, dropdown values by column, column letter by column)."""
    import openpyxl
    from openpyxl.utils import get_column_letter, range_boundaries
    wb = openpyxl.load_workbook(CATALOGUE_TEMPLATE)
    ws = wb.worksheets[0]
    cols = [c.value for c in ws[HEADER_ROW]]
    sources = {c: ws.cell(SOURCE_ROW, i + 1).value or "" for i, c in enumerate(cols)}
    letters = {c: get_column_letter(i + 1) for i, c in enumerate(cols)}
    lists = {}
    L = wb["Lists"]
    for col in L.iter_cols(min_row=1):
        key = col[0].value
        vals = [c.value for c in col[1:] if c.value not in (None, "")]
        if key:
            lists[col[0].column_letter] = vals
    dropdowns: dict[str, list[str]] = {}
    for dv in ws.data_validations.dataValidation:
        m = re.match(r"Lists!\$([A-Z]+)\$", dv.formula1 or "")
        if not m:
            continue
        for rng in str(dv.sqref).split():
            min_col, _, max_col, _ = range_boundaries(rng)
            for ci in range(min_col, max_col + 1):
                dropdowns[cols[ci - 1]] = lists.get(m.group(1), [])
    return cols, sources, dropdowns, letters


def who_fills(source: str) -> str:
    return {"Supplier": "Supplier", "GildData internal": "GildData internal"}.get(source, source)


# ------------------------------------------------------------------ rows
def yes_no(v) -> str:
    if v in ("", None):
        return ""
    return "Yes" if v else "No"


def to_row(f: dict, run_id: str) -> dict:
    """Map one repo's facts to catalogue columns (script columns only)."""
    def g(k, default=""):
        v = f.get(k, default)
        return "" if v is None else v
    return {
        "Org / Group (on host)": g("org"),
        "Dataset / Repo Name": g("repo"),
        "Repo Full Name": g("full_name"),
        "Hosting Platform": PLATFORM.get(f.get("platform", ""), ""),
        "Codebase Description": g("codebase_description"),
        "# Repos in Dataset": 1 if not f.get("error") else "",
        "Repo Type": g("repo_type"),
        "Total Files": g("total_files"),
        "Source Files": g("source_files"),
        "Test Files": g("test_files"),
        "Size (KB)": g("size_kb"),
        "Physical LOC (incl. blanks & comments)": g("physical_loc"),
        "Logical LOC (excl. blanks & comments)": g("logical_loc"),
        "Data / Markup LOC": g("data_markup_loc"),
        "Non-Authored LOC (auto-gen + vendored + deps)": g("non_authored_loc"),
        "Source LOC (production)": g("source_loc"),
        "Test LOC": g("test_loc"),
        "Primary Coding Language": g("primary_language"),
        "Language Breakdown (% of corpus)": g("language_breakdown"),
        "Category": g("category"),
        "Duplication Ratio (0.00-1.00)": g("duplication_ratio"),
        "Cross-Repo Duplication Ratio": g("cross_repo_duplication_ratio"),
        "Shared-Root Group ID": g("shared_root_group_id"),
        "# Functions": g("functions"),
        "# Classes / Types": g("classes_types"),
        "Parser Coverage": g("parser_coverage"),
        "Test-to-Code LOC Ratio": g("test_to_code_ratio"),
        "% Untested Files (heuristic)": g("untested_file_share"),
        "Comment + Docstring Ratio": g("comment_docstring_ratio"),
        "Has Tests": yes_no(f.get("has_tests")),
        "Has Test Runner": yes_no(f.get("has_test_runner")),
        "Has CI/CD": yes_no(f.get("has_ci_cd")),
        "Has Library Code": yes_no(f.get("has_library_code")),
        "AI-Generated Code Signals": g("ai_signals_text"),
        "Default Branch": g("default_branch"),
        "# Branches": g("branches"),
        "# Tags": g("tags"),
        "# of Commits (total)": g("total_commits"),
        "Non-Merge Commit Count": g("non_merge_commits"),
        "First Commit Date": g("first_commit"),
        "Last Commit Date": g("last_commit"),
        "Recency (days)": g("recency_days"),
        "# of Contributors": g("contributors"),
        "Bot Authors": g("bot_authors"),
        "Bot Commit Ratio": g("bot_commit_ratio"),
        "Snapshot Commit SHA": (g("snapshot_sha") or "")[:7],
        "# of PRs (total)": g("prs_total"),
        "# of Merged PRs": g("prs_merged"),
        "# of Reviewed PRs": g("prs_reviewed"),
        "Avg LoC per PR": g("avg_loc_per_pr"),
        "% Simple PRs": g("pr_simple"),
        "% Standard PRs": g("pr_standard"),
        "% Rich PRs": g("pr_rich"),
        "PR Data Source": g("pr_data_source"),
        "Build Command": g("build_command"),
        "Test Command": g("test_command"),
        "Runtime / Toolchain Version": g("runtime"),
        "Containerized": yes_no(f.get("containerized")),
        "Known Open-Source / Forked Components": g("oss_hint_text"),
        "Secrets Scan Result": g("secrets_result", "Not run"),
        "PII Scan Result": g("pii_result", "Not run"),
        "Archive / Delivery File Name": (f"{f.get('org')}-{f.get('repo')}.tar.gz"
                                         if f.get("repo") else ""),
        "Extraction Run ID / Date": run_id,
    }


# Columns the script only suggests: a supplier answer replaces them without a conflict.
SUGGESTED = {"Build Command", "Test Command", "Runtime / Toolchain Version",
             "Known Open-Source / Forked Components", "Codebase Description", "Repo Type"}


# ------------------------------------------------------------------ answers
def read_answers(path: Path, columns: list[str]) -> list[dict]:
    """Our catalogue template (xlsx) or a CSV with the same headers."""
    rows = []
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                rows.append({k.strip(): (v or "").strip() for k, v in r.items() if k})
        return [r for r in rows if any(r.values())]
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    header_row = None
    for i in range(1, 12):
        vals = [str(c.value).strip() if c.value is not None else "" for c in ws[i]]
        if "Repo Full Name" in vals:
            header_row, header = i, vals
            break
    if header_row is None:
        raise SystemExit(f"{path}: no 'Repo Full Name' header in the first rows")
    start = header_row + 4 if header_row == HEADER_ROW else header_row + 1
    for r in ws.iter_rows(min_row=start, values_only=True):
        row = {h: ("" if v is None else v) for h, v in zip(header, r) if h}
        if not any(str(v).strip() for v in row.values()):
            continue
        if "SAMPLE ROW" in str(row.get("Notes", "")).upper():
            continue
        rows.append(row)
    return rows


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def merge_answers(rows: list[dict], answers: list[dict], sources: dict, dropdowns: dict,
                  qc: list, missing: list) -> None:
    by_full = {r["Repo Full Name"].lower(): r for r in rows if r.get("Repo Full Name")}
    org_company: dict[str, dict] = defaultdict(dict)
    for a in answers:
        full = str(a.get("Repo Full Name", "")).strip().lower()
        target = by_full.get(full)
        if target is None and a.get("Dataset ID"):
            target = next((r for r in rows if r.get("Dataset ID") == a["Dataset ID"]), None)
        if target is None:
            missing.append({"repo": a.get("Repo Full Name") or a.get("Dataset ID") or "?",
                            "column": "(whole row)",
                            "who_fills": "Answer row matched no extracted repo"})
            continue
        for col, val in a.items():
            if col not in sources or str(val).strip() == "":
                continue
            val = val.strip() if isinstance(val, str) else val
            if col in dropdowns and dropdowns[col] and str(val) not in [str(x) for x in dropdowns[col]]:
                qc.append({"repo": target["Repo Full Name"], "flag": "INVALID VALUE", "value": col,
                           "rule": f"'{val}' is not in the template dropdown (rejected)"})
                continue
            src = sources[col]
            if src.startswith("Script") and col not in SUGGESTED:
                ours = target.get(col, "")
                if ours not in ("", None) and str(ours) != str(val):
                    a_n, o_n = _num(val), _num(ours)
                    if a_n is not None and o_n is not None and o_n and abs(a_n - o_n) / abs(o_n) < 0.01:
                        continue
                    qc.append({"repo": target["Repo Full Name"], "flag": "CONFLICT", "value": col,
                               "rule": f"supplier says {val}; script says {ours} (script value kept)"})
                    continue
                if ours in ("", None):
                    target[col] = val
                continue
            target[col] = val
            if col in COMPANY_COLUMNS:
                org_company[target["Org / Group (on host)"]].setdefault(col, val)
    for r in rows:   # company fields entered once per org apply to all of its repos
        for col, val in org_company.get(r.get("Org / Group (on host)"), {}).items():
            if r.get(col) in ("", None):
                r[col] = val


# ------------------------------------------------------------------ bundles
def bundle_rows(rows: list[dict], facts: dict[str, dict], bundle_dup: dict[str, float]) -> list[dict]:
    """Repos that share a Dataset ID (from the answers file) become one row."""
    groups: dict[str, list[dict]] = defaultdict(list)
    singles = []
    for r in rows:
        if r.get("Dataset ID"):
            groups[r["Dataset ID"]].append(r)
        else:
            singles.append(r)
    out = []
    for did, members in groups.items():
        if len(members) == 1:
            out.append(members[0])
            continue
        b = dict(members[0])
        b["Repo Full Name"] = "; ".join(m["Repo Full Name"] for m in members)
        b["Dataset / Repo Name"] = members[0].get("Dataset / Repo Name") if all(
            m.get("Dataset / Repo Name") == members[0].get("Dataset / Repo Name") for m in members) \
            else did
        for c in SUM_COLS:
            vals = [_num(m.get(c)) for m in members]
            b[c] = int(sum(v for v in vals if v is not None)) if any(v is not None for v in vals) else ""

        def wavg(col, weight):
            pairs = [(_num(m.get(col)), _num(m.get(weight)) or 0) for m in members]
            pairs = [(v, w) for v, w in pairs if v is not None]
            tw = sum(w for _, w in pairs)
            return round(sum(v * w for v, w in pairs) / tw, 4) if tw else ""
        b["Duplication Ratio (0.00-1.00)"] = bundle_dup.get(did, wavg(
            "Duplication Ratio (0.00-1.00)", "Logical LOC (excl. blanks & comments)"))
        b["Cross-Repo Duplication Ratio"] = wavg("Cross-Repo Duplication Ratio",
                                                 "Logical LOC (excl. blanks & comments)")
        b["Parser Coverage"] = wavg("Parser Coverage", "Source LOC (production)")
        b["Comment + Docstring Ratio"] = wavg("Comment + Docstring Ratio", "Source LOC (production)")
        b["% Untested Files (heuristic)"] = wavg("% Untested Files (heuristic)", "Source Files")
        b["Bot Commit Ratio"] = wavg("Bot Commit Ratio", "# of Commits (total)")
        b["Avg LoC per PR"] = wavg("Avg LoC per PR", "# of Merged PRs")
        for c in ("% Simple PRs", "% Standard PRs", "% Rich PRs"):
            b[c] = wavg(c, "# of Merged PRs")
        src, tst = _num(b.get("Source LOC (production)")), _num(b.get("Test LOC"))
        b["Test-to-Code LOC Ratio"] = round(tst / src, 4) if src else ""
        langs: dict[str, int] = defaultdict(int)
        authors: set = set()
        for m in members:
            fx = facts.get(m["Repo Full Name"], {})
            for k, v in (fx.get("lang_code_loc") or {}).items():
                langs[k] += v
            authors |= set(fx.get("_author_keys") or [])
        if langs:
            tot = sum(langs.values())
            b["Primary Coding Language"] = max(langs.items(), key=lambda kv: kv[1])[0]
            b["Language Breakdown (% of corpus)"] = "; ".join(
                f"{k}:{v / tot * 100:.1f}%" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])
                if v / tot >= 0.001)
        if authors:
            b["# of Contributors"] = len(authors)
        dates = [m.get("First Commit Date") for m in members if m.get("First Commit Date")]
        b["First Commit Date"] = min(dates) if dates else ""
        dates = [m.get("Last Commit Date") for m in members if m.get("Last Commit Date")]
        b["Last Commit Date"] = max(dates) if dates else ""
        rec = [_num(m.get("Recency (days)")) for m in members]
        rec = [x for x in rec if x is not None]
        b["Recency (days)"] = int(min(rec)) if rec else ""
        for c in ("Repo Type", "Default Branch", "Shared-Root Group ID", "Snapshot Commit SHA",
                  "Codebase Description", "AI-Generated Code Signals", "PR Data Source",
                  "Known Open-Source / Forked Components", "Secrets Scan Result", "PII Scan Result",
                  "Has Tests", "Has Test Runner", "Has CI/CD", "Has Library Code", "Containerized"):
            vals = list(dict.fromkeys(str(m.get(c)) for m in members if m.get(c) not in ("", None)))
            if c.startswith("Has ") or c == "Containerized":
                b[c] = "Yes" if "Yes" in vals else ("No" if vals else "")
            elif c in ("Secrets Scan Result", "PII Scan Result"):
                b[c] = "Findings - open" if "Findings - open" in vals else (vals[0] if vals else "")
            else:
                b[c] = "; ".join(vals)[:500]
        b["Archive / Delivery File Name"] = "; ".join(m["Archive / Delivery File Name"] for m in members)
        b["_members"] = [m["Repo Full Name"] for m in members]
        out.append(b)
    return singles + out


# ------------------------------------------------------------------ formulas
def formula_values(r: dict) -> dict:
    """Python copies of the template's formula columns, for summary.csv."""
    out = {}
    est, closed = _num(r.get("Established Year")), _num(r.get("Closed Year"))
    out["Years in Business"] = int((closed or datetime.now().year) - est) if est else ""
    loc, na = _num(r.get("Logical LOC (excl. blanks & comments)")), _num(
        r.get("Non-Authored LOC (auto-gen + vendored + deps)")) or 0
    dup = _num(r.get("Duplication Ratio (0.00-1.00)")) or 0
    out["Net Authored LOC"] = int(loc - na) if loc is not None else ""
    out["Est. Effective Private Tokens (M)"] = round((loc - na) * (1 - dup) * 10.35 / 1e6, 6) \
        if loc is not None else ""
    a, b = parse_date(str(r.get("First Commit Date") or "")), parse_date(str(r.get("Last Commit Date") or ""))
    out["Active Span (days)"] = (b - a).days if a and b else ""
    fmt, builds = r.get("Git Delivery Format", ""), r.get("Builds & Tests", "")
    if fmt:
        full = fmt in ("Full .git (all branches + tags)", "Full .git (default branch only)")
        out["Price Tier Multiplier"] = (1 if builds == "Builds + test suite passes" else 0.7) if full else 0.4
    else:
        out["Price Tier Multiplier"] = ""
    price, tok = _num(r.get("Quoted Price (USD)")), out["Est. Effective Private Tokens (M)"]
    out["Implied USD per M Tokens"] = round(price / tok, 4) if price and tok else ""
    flags = []
    if r.get("Duplication Ratio (0.00-1.00)") not in ("", None) and dup == 0:
        flags.append("ZERO DUP")
    nm = _num(r.get("Non-Merge Commit Count"))
    if nm is not None and nm <= 5:
        flags.append("SHALLOW")
    if r.get("Private Code Attestation") == "Previously public, now private":
        flags.append("PUBLIC RISK")
    if na > (loc or 0):
        flags.append("NA>LOC")
    out["Client QC Flags"] = " ".join(flags) if r.get("Dataset / Repo Name") else ""
    return out


# ------------------------------------------------------------------ QC
def qc_flags(rows: list[dict], facts: dict, cfg: dict, dup_groups: list[dict]) -> list[dict]:
    out = []
    in_group = defaultdict(list)
    for g in dup_groups:
        for repo in g["repos"].split(";"):
            in_group[repo].append(g)
    for r in rows:
        name = r["Repo Full Name"]
        loc = _num(r.get("Logical LOC (excl. blanks & comments)")) or 0
        commits = _num(r.get("# of Commits (total)")) or 0
        if not loc or not commits:
            out.append({"repo": name, "flag": "EMPTY", "value": int(loc), "rule": "Logical LOC is 0 or no commits"})
        nm = _num(r.get("Non-Merge Commit Count"))
        if nm is not None and nm <= cfg["shallow_commits"]:
            out.append({"repo": name, "flag": "SHALLOW", "value": int(nm),
                        "rule": f"non-merge commits <= {cfg['shallow_commits']}"})
        d = r.get("Duplication Ratio (0.00-1.00)")
        if d not in ("", None) and _num(d) == 0 and loc:
            out.append({"repo": name, "flag": "ZERO DUP", "value": 0, "rule": "duplication ratio is exactly 0.00 (buyers audit it)"})
        na = _num(r.get("Non-Authored LOC (auto-gen + vendored + deps)")) or 0
        if na > loc:
            out.append({"repo": name, "flag": "NA>LOC", "value": int(na), "rule": "Non-Authored LOC higher than Logical LOC"})
        dm = _num(r.get("Data / Markup LOC")) or 0
        if loc and dm / loc > cfg["data_heavy_share"]:
            out.append({"repo": name, "flag": "DATA HEAVY", "value": round(dm / loc, 2),
                        "rule": f"data/markup LOC > {int(cfg['data_heavy_share'] * 100)}% of logical LOC"})
        for g in in_group.get(name, []):
            out.append({"repo": name, "flag": "DUPLICATE GROUP", "value": g["group_id"],
                        "rule": f"{g['method']} with " + ";".join(x for x in g["repos"].split(";") if x != name)})
        cx = _num(r.get("Cross-Repo Duplication Ratio"))
        if cx is not None and cx > 0.8 and not in_group.get(name):
            out.append({"repo": name, "flag": "DUPLICATE GROUP", "value": cx, "rule": "over 80% cross-repo duplication"})
        oss = r.get("Known Open-Source / Forked Components") or ""
        if re.search(r"fork|upstream|\(repo root\)", oss) or r.get("Private Code Attestation") == "Previously public, now private":
            out.append({"repo": name, "flag": "PUBLIC RISK", "value": oss[:120] or "attestation",
                        "rule": "OSS fork hint, or attestation says previously public"})
        for col, label in (("Secrets Scan Result", "SECRETS"), ("PII Scan Result", "PII")):
            if r.get(col) == "Findings - open":
                f = facts.get(name, {})
                n = f.get("secrets_count") if label == "SECRETS" else f.get("pii_count")
                out.append({"repo": name, "flag": label, "value": n or "open",
                            "rule": "open finding (see evidence/<repo>/secrets_pii.json)"})
        db = r.get("Default Branch") or ""
        if db and not any(b == db for b in cfg["default_branch_names"]) and ";" not in db:
            out.append({"repo": name, "flag": "FEATURE BRANCH", "value": db,
                        "rule": "default branch isn't main, master, develop or trunk"})
        if loc and not r.get("Codebase Description"):
            out.append({"repo": name, "flag": "NO README", "value": "", "rule": "no README paragraph to describe the repo"})
        pc = _num(r.get("Parser Coverage"))
        if pc is not None and pc < 0.9 and loc:
            out.append({"repo": name, "flag": "LOW PARSER COVERAGE", "value": pc,
                        "rule": "below 0.9: function and class counts are lower bounds"})
        fx = facts.get(name, {})
        if fx.get("error"):
            out.append({"repo": name, "flag": "EXTRACT FAILED", "value": fx.get("error_class", ""),
                        "rule": "see logs/failures.json"})
    return out


def missing_fields(rows: list[dict], columns: list[str], sources: dict) -> list[dict]:
    out = []
    for r in rows:
        for c in columns:
            src = sources.get(c, "")
            if src.startswith("Formula") or r.get(c) not in ("", None):
                continue
            if src.startswith("Script"):
                who = ("Supplier export (offline run, no PR API)" if c in (
                    "# of PRs (total)", "# of Reviewed PRs") else "Script (could not compute)")
            elif c == "Closed Year":
                who = "Supplier (blank = still operating)"
            else:
                who = who_fills(src)
            out.append({"repo": r["Repo Full Name"], "column": c, "who_fills": who})
    return out


# ------------------------------------------------------------------ writers
def _excel_value(col: str, v):
    if v in ("", None):
        return None
    if col in ("First Commit Date", "Last Commit Date"):
        d = parse_date(str(v))
        if d is None:
            return v
        return d.astimezone(timezone.utc).replace(tzinfo=None) if d.tzinfo else d
    if isinstance(v, (int, float)):
        return v
    n = _num(v)
    if n is not None and re.fullmatch(r"-?[\d,]+(\.\d+)?", str(v).strip()) and col not in (
            "Snapshot Commit SHA", "Shared-Root Group ID", "Dataset ID", "Extraction Run ID / Date"):
        return int(n) if n.is_integer() else n
    return v


def write_catalogue(rows: list[dict], path: Path, columns: list[str], sources: dict) -> None:
    import openpyxl
    from copy import copy
    wb = openpyxl.load_workbook(CATALOGUE_TEMPLATE)
    ws = wb.worksheets[0]
    last_template = ws.max_row
    for i, r in enumerate(rows):
        row = FIRST_DATA_ROW + i
        for ci, col in enumerate(columns, start=1):
            cell = ws.cell(row, ci)
            if sources.get(col, "").startswith("Formula"):
                if cell.value is None and row > last_template:   # extend formulas past row 508
                    src = ws.cell(FIRST_DATA_ROW, ci).value
                    if isinstance(src, str):
                        cell.value = re.sub(rf"(?<=[A-Z]){FIRST_DATA_ROW}\b", str(row), src)
                        cell._style = copy(ws.cell(FIRST_DATA_ROW, ci)._style)
                continue
            cell.value = _excel_value(col, r.get(col))
            if col in ("First Commit Date", "Last Commit Date") and cell.value is not None:
                cell.number_format = "yyyy-mm-dd hh:mm"
    wb.save(path)


def write_summary(rows: list[dict], path: Path, columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def write_meta(rows: list[dict], path: Path) -> None:
    import openpyxl
    wb = openpyxl.load_workbook(META_TEMPLATE)
    ws = wb["Vendor Quote Form"]
    keys = ["Dataset ID", "Vendor Name", "Originating Company (legal name)",
            "Originating Company's Industry", "Dataset / Repo Name", "# Repos in Dataset",
            "Logical LOC (excl. blanks & comments)", "Non-Authored LOC (auto-gen + vendored + deps)",
            "Duplication Ratio (0.00-1.00)", "Non-Merge Commit Count", "Git Delivery Format",
            "Builds & Tests", "Private Code Attestation", "Quoted Price (USD)"]
    rows = [r for r in rows if r.get("Include in Quote?") != "Exclude"]
    for i, r in enumerate(rows):
        for ci, k in enumerate(keys, start=1):
            ws.cell(2 + i, ci).value = _excel_value(k, r.get(k))
    wb.save(path)


def _move_merged_below(ws, first_row: int, n_rows: int) -> None:
    """The buyer Metric Notes tab has a merged footer note a few rows under the header. Writing into
    a merged cell raises in openpyxl, so any merged range inside the data area is moved to just
    after the last data row (text and merge kept). Seen with 109 repos; the template fits 11."""
    last = first_row + n_rows - 1
    for rng in [r for r in ws.merged_cells.ranges if r.min_row >= first_row and r.min_row <= last]:
        text = ws.cell(rng.min_row, rng.min_col).value
        ws.unmerge_cells(str(rng))
        for row in ws.iter_rows(min_row=rng.min_row, max_row=rng.max_row, min_col=rng.min_col, max_col=rng.max_col):
            for c in row:
                c.value = None
        new_row = last + 2 + (rng.min_row - first_row) // max(1, n_rows)
        ws.cell(new_row, rng.min_col).value = text
        ws.merge_cells(start_row=new_row, start_column=rng.min_col, end_row=new_row, end_column=rng.max_col)


def write_hud(rows: list[dict], path: Path) -> None:
    import openpyxl
    wb = openpyxl.load_workbook(HUD_TEMPLATE)
    ev = wb["Evaluation"]
    ev_cols = {2: "Originating Company (legal name)", 3: "Sub-Company / Project Name",
               4: "Originating Company's Industry", 5: "Description of Business",
               6: "Years in Business", 7: "Established Year", 8: "Closed Year",
               9: "# of Employees", 10: "# of Contributors", 11: "Quality of SWE", 12: "Category",
               13: "Primary Coding Language", 14: "Logical LOC (excl. blanks & comments)",
               15: "# Repos in Dataset", 16: "# of PRs (total)", 17: "# of Merged PRs",
               18: "Avg LoC per PR", 19: "Language Breakdown (% of corpus)", 20: "% Simple PRs",
               21: "% Standard PRs", 22: "% Rich PRs", 23: "# of Commits (total)",
               24: "# Functions", 25: "# Classes / Types", 26: "Test-to-Code LOC Ratio",
               27: "% Untested Files (heuristic)", 28: "Comment + Docstring Ratio",
               29: "Sample Code Available", 30: "References", 31: "Business Systems Covered"}
    pii = wb["PII clean up status"]
    mn = wb["Metric Notes"]
    rows = [r for r in rows if r.get("Include in Quote?") != "Exclude"]
    _move_merged_below(mn, first_row=12, n_rows=len(rows))
    for i, r in enumerate(rows):
        for ci, k in ev_cols.items():
            v = r.get(k)
            if k == "Sample Code Available" and v == "Yes":
                v = "Available on request (sanitized)"
            ev.cell(4 + i, ci).value = _excel_value(k, v)
        pii.cell(4 + i, 2).value = r.get("Originating Company (legal name)") or None
        pii.cell(4 + i, 3).value = r.get("Dataset / Repo Name") or None
        pii.cell(4 + i, 5).value = r.get("Vendor Claimed PII Cleaned?") or None
        pii.cell(4 + i, 6).value = r.get("PII Verified Clean?") or None
        pii.cell(4 + i, 9).value = (f"Secrets scan: {r.get('Secrets Scan Result') or 'Not run'}; "
                                    f"PII scan: {r.get('PII Scan Result') or 'Not run'}")
        vals = [r.get("Category"), r.get("Originating Company (legal name)"),
                r.get("Dataset / Repo Name"), "Exact" if r.get("Snapshot Commit SHA") else "",
                r.get("Source Files"), r.get("Test Files"), r.get("Source LOC (production)"),
                r.get("Test LOC"), r.get("_matched_source_files"), r.get("Parser Coverage"),
                r.get("Archive / Delivery File Name"),
                f"Metrics computed on snapshot {r.get('Snapshot Commit SHA')}" if r.get("Snapshot Commit SHA") else ""]
        for ci, v in enumerate(vals, start=1):
            mn.cell(12 + i, ci).value = _excel_value("", v) if v not in ("", None) else None
    wb.save(path)


def write_csv(rows: list[dict], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def column_counts(sources: dict) -> dict:
    c = defaultdict(int)
    for s in sources.values():
        c["script" if s.startswith("Script") else "supplier" if s == "Supplier"
          else "gildata" if s == "GildData internal" else "formula"] += 1
    return dict(c)
