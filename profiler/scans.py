"""Secrets and PII scans, build hints, open source hints, repo type and the
README description. Findings store type, path and count only, never values."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from .util import log, run

SECRET_RULES = {
    "aws-access-key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY( BLOCK)?-----"),
    "github-token": re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    "gitlab-token": re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    "slack-token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    "slack-webhook": re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    "stripe-live-key": re.compile(r"\b(sk|rk)_live_[0-9a-zA-Z]{20,}\b"),
    "razorpay-key": re.compile(r"\brzp_live_[0-9a-zA-Z]{10,}\b"),
    "openai-key": re.compile(r"\bsk-(proj-)?[A-Za-z0-9_\-]{32,}\b"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{32,}\b"),
    "twilio-key": re.compile(r"\bSK[0-9a-fA-F]{32}\b"),
    "sendgrid-key": re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "db-url-with-password": re.compile(
        r"\b(postgres(ql)?|mysql|mongodb(\+srv)?|redis|amqp|mssql)://[^:\s/@]+:[^@\s/]{3,}@"),
    "generic-secret-assignment": re.compile(
        r"""(?i)\b(password|passwd|pwd|secret|secret_key|api_key|apikey|access_key|auth_token|"""
        r"""client_secret|private_key)\b["']?\s*[:=]\s*["']([^"'\s$<{]{8,})["']"""),
}
PLACEHOLDER_RE = re.compile(r"(?i)(change.?me|example|dummy|sample|placeholder|your[_-]|xxx|"
                            r"\*\*\*|test|fake|secret123|password123|<|\$\{|%s|todo)")

PII_RULES = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone-india": re.compile(r"(?<![\w+])(\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),
    "phone-intl": re.compile(r"(?<![\w])\+(?!91)[1-9]\d{0,2}[\s-]?\(?\d{2,4}\)?[\s-]?\d{3,4}[\s-]?\d{3,4}(?!\d)"),
    "aadhaar": re.compile(r"(?<!\d)[2-9]\d{3}[\s-]\d{4}[\s-]\d{4}(?!\d)"),
    "pan": re.compile(r"\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b"),
}
SAFE_EMAIL_RE = re.compile(r"(?i)@(example\.(com|org|net)|test\.com|localhost|users\.noreply\.github\.com|"
                           r"noreply|domain\.com|email\.com|company\.com|sentry\.io)|^(noreply|no-reply|"
                           r"user|test|admin|john\.doe|jane\.doe|foo|bar)@")


def secrets_scan(repo: Path, files: list[dict], history: bool, max_bytes: int) -> dict:
    """gitleaks when installed, else built in rules. Working tree, plus history if asked."""
    if shutil.which("gitleaks"):
        res = _gitleaks(repo, history)
        if res is not None:
            return res
    found: dict[tuple[str, str], int] = Counter()
    for r in files:
        if r["class"] == "binary":
            continue
        p = repo / r["path"]
        try:
            if p.stat().st_size > max_bytes:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rule, rx in SECRET_RULES.items():
            for m in rx.finditer(text):
                if rule == "generic-secret-assignment" and PLACEHOLDER_RE.search(m.group(2)):
                    continue
                if rule == "openai-key" and m.group(0).startswith("sk-ant-"):
                    continue
                found[(rule, r["path"])] += 1
    hist = {}
    if history:
        hist = _history_regex(repo)
    findings = [{"rule": k[0], "file": k[1], "count": v} for k, v in sorted(found.items())]
    total = sum(found.values()) + sum(hist.values())
    return {"tool": "builtin rules (gitleaks not installed)",
            "scope": "HEAD + history" if history else "HEAD working tree",
            "findings": total, "by_file": findings[:500],
            "history_by_rule": hist, "status": "Findings - open" if total else "Clean"}


def _gitleaks(repo: Path, history: bool) -> dict | None:
    out = Path(tempfile.mkdtemp(prefix="gdp-gl-")) / "r.json"
    try:
        args = ["gitleaks", "detect", "--source", str(repo), "--report-format", "json",
                "--report-path", str(out), "--redact", "--exit-code", "0"]
        if not history:
            args.append("--no-git")
        run(args, timeout=1800, check=False)
        data = json.loads(out.read_text()) if out.exists() else []
        ver = run(["gitleaks", "version"], check=False).strip()
        by = Counter((d.get("RuleID", "?"), d.get("File", "")) for d in data)
        return {"tool": f"gitleaks {ver}", "scope": "HEAD + history" if history else "HEAD working tree",
                "findings": len(data),
                "by_file": [{"rule": k[0], "file": k[1].replace(str(repo), "").lstrip("/\\"), "count": v}
                            for k, v in sorted(by.items())][:500],
                "status": "Findings - open" if data else "Clean"}
    except Exception as e:  # noqa: BLE001
        log.warning("gitleaks failed: %s", e)
        return None
    finally:
        shutil.rmtree(out.parent, ignore_errors=True)


def _history_regex(repo: Path) -> dict:
    out = run(["git", "log", "--all", "-p", "--no-color", "--unified=0", "--format=%H"],
              cwd=repo, timeout=3600, check=False)
    found: Counter = Counter()
    for line in out.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for rule, rx in SECRET_RULES.items():
            m = rx.search(line)
            if m and not (rule == "generic-secret-assignment" and PLACEHOLDER_RE.search(m.group(2))):
                found[rule] += 1
    return dict(found)


def pii_scan(repo: Path, files: list[dict], max_bytes: int, author_emails: int) -> dict:
    found: dict[tuple[str, str], int] = Counter()
    for r in files:
        if r["class"] in ("binary", "vendored", "generated"):
            continue
        p = repo / r["path"]
        try:
            if p.stat().st_size > max_bytes:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        is_data = r["class"] == "data" or "fixture" in r["path"].lower()
        for kind, rx in PII_RULES.items():
            if kind == "aadhaar" and not is_data and "aadhaar" not in text.lower():
                continue
            n = 0
            for m in rx.finditer(text):
                if kind == "email" and SAFE_EMAIL_RE.search(m.group(0)):
                    continue
                n += 1
            if n:
                found[(kind, r["path"])] += n
    findings = [{"type": k[0], "file": k[1], "count": v, "status": "open"}
                for k, v in sorted(found.items(), key=lambda kv: -kv[1])]
    by_type = Counter()
    for (k, _), v in found.items():
        by_type[k] += v
    total = sum(found.values())
    return {"tool": "regex (email, phone incl. +91, Aadhaar, PAN)", "findings_total": total,
            "by_type": dict(by_type), "findings": findings[:500],
            "author_emails_in_history": author_emails,
            "status": "Findings - open" if total else "Clean",
            "note": "Values are never stored, only type, path and count."}


# ------------------------------------------------------------ build hints
def build_hints(repo: Path, files: list[str]) -> dict:
    top = {f for f in files if "/" not in f}

    def read(f, n=200_000):
        try:
            return (repo / f).read_text(encoding="utf-8", errors="ignore")[:n]
        except OSError:
            return ""
    build = test = ""
    runtime = []
    if "gradlew" in top or "build.gradle" in top or "build.gradle.kts" in top:
        g = "./gradlew" if "gradlew" in top else "gradle"
        build, test = f"{g} build", f"{g} test"
        wp = read("gradle/wrapper/gradle-wrapper.properties")
        m = re.search(r"gradle-([\d.]+)-", wp)
        bg = read("build.gradle") + read("build.gradle.kts")
        j = re.search(r"languageVersion\s*(=|\.set\()\s*JavaLanguageVersion\.of\((\d+)\)", bg) or \
            re.search(r"sourceCompatibility\s*=\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)", bg)
        if j:
            runtime.append(f"Java {j.groups()[-1]}")
        if m:
            runtime.append(f"Gradle {m.group(1)}")
    elif "pom.xml" in top:
        build, test = ("./mvnw -B package" if "mvnw" in top else "mvn -B package"), "mvn test"
        pom = read("pom.xml")
        j = re.search(r"<(maven\.compiler\.(release|source)|java\.version)>(\d+)", pom)
        if j:
            runtime.append(f"Java {j.group(3)}")
        runtime.append("Maven")
    if "package.json" in top:
        try:
            pkg = json.loads(read("package.json"))
        except ValueError:
            pkg = {}
        pm = "pnpm" if "pnpm-lock.yaml" in top else "yarn" if "yarn.lock" in top else "npm"
        scripts = pkg.get("scripts") or {}
        if not build:
            build = f"{pm} install && {pm} run build" if "build" in scripts else f"{pm} install"
        if not test and scripts.get("test") and "no test specified" not in scripts["test"]:
            test = f"{pm} test"
        node = (read(".nvmrc") or read(".node-version")).strip() or (pkg.get("engines") or {}).get("node", "")
        if node:
            runtime.append(f"Node {node}")
    if top & {"pyproject.toml", "setup.py", "requirements.txt", "Pipfile"}:
        py = read("pyproject.toml")
        if not build:
            build = ("poetry install" if "[tool.poetry]" in py else
                     "pip install -r requirements.txt" if "requirements.txt" in top else "pip install -e .")
        if not test:
            test = "pytest" if any("test" in f for f in files) else ""
        v = read(".python-version").strip() or (re.search(r'requires-python\s*=\s*"([^"]+)"', py) or [None, ""])[1]
        if v:
            runtime.append(f"Python {v}")
    if "go.mod" in top:
        build, test = build or "go build ./...", test or "go test ./..."
        m = re.search(r"^go\s+([\d.]+)", read("go.mod"), re.M)
        if m:
            runtime.append(f"Go {m.group(1)}")
    if "Cargo.toml" in top:
        build, test = build or "cargo build --release", test or "cargo test"
        tc = read("rust-toolchain") or read("rust-toolchain.toml")
        m = re.search(r"([\d]+\.[\d]+(\.[\d]+)?)", tc)
        runtime.append(f"Rust {m.group(1)}" if m else "Rust")
    if "composer.json" in top:
        build, test = build or "composer install", test or "vendor/bin/phpunit"
        runtime.append("PHP")
    if "Gemfile" in top:
        build = build or "bundle install"
        test = test or ("bundle exec rspec" if any(f.startswith("spec/") for f in files) else "bundle exec rake test")
        rv = read(".ruby-version").strip()
        runtime.append(f"Ruby {rv}" if rv else "Ruby")
    if "Makefile" in top:
        mk = read("Makefile")
        if not build and re.search(r"^build:", mk, re.M):
            build = "make build"
        if not test and re.search(r"^test:", mk, re.M):
            test = "make test"
    if any(f.endswith(".csproj") or f.endswith(".sln") for f in files):
        build, test = build or "dotnet build", test or "dotnet test"
        runtime.append(".NET")
    docker = any(re.search(r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose\.ya?ml)$", f)
                 and f.count("/") <= 2 for f in files)
    return {"build_command": build, "test_command": test, "runtime": ", ".join(dict.fromkeys(runtime)),
            "containerized": docker, "status": "suggested - supplier to confirm"}


LICENSE_RE = re.compile(r"(?i)\b(MIT License|Apache License|GNU (Lesser |Affero )?General Public License|"
                        r"BSD [0-9]-Clause|Mozilla Public License|ISC License|The Unlicense|"
                        r"Eclipse Public License)")


def oss_hints(repo: Path, rows: list[dict], meta: dict | None, remotes: str) -> list[dict]:
    out = []
    lic_files = [r["path"] for r in rows if re.search(r"(^|/)(LICEN[CS]E|COPYING)(\.[a-z]+)?$", r["path"], re.I)]
    for f in lic_files:
        try:
            text = (repo / f).read_text(encoding="utf-8", errors="ignore")[:4000]
        except OSError:
            continue
        m = LICENSE_RE.search(text)
        if m:
            where = f.rsplit("/", 1)[0] if "/" in f else "(repo root)"
            out.append({"path": where, "reason": f"{m.group(1)} licence file"})
    vend = sorted({r["path"].split("/")[0] for r in rows if r["class"] == "vendored"})
    for v in vend[:20]:
        out.append({"path": v, "reason": "vendored third party folder"})
    if meta and (meta.get("fork") or meta.get("parent") or meta.get("forked_from_project")):
        out.append({"path": "(repo)", "reason": "marked as a fork on the platform"})
    if re.search(r"^upstream\s", remotes, re.M):
        out.append({"path": "(repo)", "reason": "has an upstream remote"})
    return out


TRAINING_RE = re.compile(r"(?i)(^|[-_.])(module|stub|solution|exercise|assignment|lab|labs|tutorial|course|"
                         r"homework|practice|kata|bootcamp|workshop|challenge|lesson|starter)([-_.\d]|$)")
SCRATCH_RE = re.compile(r"(?i)(^|[-_.])(poc|demo|sandbox|playground|scratch|experiment|hello|test|temp|tmp|"
                        r"trial|sample|prototype)([-_.\d]|$)")
SERVER_RE = re.compile(r"(?i)\b(express|fastify|koa|@nestjs|hapi|django|flask|fastapi|tornado|spring-boot|"
                       r"springframework|gin-gonic|echo|fiber|actix|rocket|axum|rails|sinatra|laravel|"
                       r"symfony|ktor|micronaut|quarkus|aspnetcore|Microsoft\.AspNetCore)\b")
FRONT_RE = re.compile(r'"(react|next|vue|nuxt|@angular/core|svelte|@sveltejs/kit|solid-js|gatsby|remix)"\s*:')
MOBILE_RE = re.compile(r'(?i)"react-native"\s*:|com\.android\.application|flutter:')
DATA_RE = re.compile(r"(?i)\b(pandas|pyspark|apache-airflow|airflow|dbt-core|torch|tensorflow|scikit-learn|"
                     r"sklearn|xgboost|lightgbm|keras|transformers|polars)\b")


def repo_type(name: str, rows: list[dict], vol: dict, has_lib: bool, repo: Path) -> str:
    """Rule based guess from the template dropdown."""
    if not vol["logical_loc"]:
        return "Empty"
    paths = [r["path"] for r in rows]
    top = {p for p in paths if "/" not in p}

    def read(f):
        try:
            return (repo / f).read_text(encoding="utf-8", errors="ignore")[:200_000]
        except OSError:
            return ""
    manifests = "".join(read(f) for f in ("package.json", "requirements.txt", "pyproject.toml",
                                          "pom.xml", "build.gradle", "build.gradle.kts", "go.mod",
                                          "Gemfile", "composer.json", "Cargo.toml") if f in top)
    if TRAINING_RE.search(name):
        return "Training exercise"
    code_langs = vol["lang_code_loc"]
    prod = sum(code_langs.values())
    if not prod:
        return "Config only"
    infra = sum(v for k, v in code_langs.items() if k in ("Terraform", "HCL", "Shell", "BASH", "Dockerfile",
                                                           "Makefile", "PowerShell", "Nix"))
    if infra / prod > 0.6 or any(p.endswith(".tf") for p in paths) and infra / prod > 0.3:
        return "Infra / DevOps"
    own_pkg = re.search(r'(?im)^\s*name\s*=\s*["\']([^"\']+)["\']|"name"\s*:\s*"([^"]+)"|<artifactId>([^<]+)</artifactId>', manifests)
    pkg_name = next((g for g in (own_pkg.groups() if own_pkg else []) if g), "").lower().split("/")[-1]
    has_docker = any(re.search(r"(^|/)Dockerfile", p) for p in paths)
    if has_lib and pkg_name and pkg_name.replace("_", "-") == name.lower().replace("_", "-") and not has_docker \
            and not FRONT_RE.search(manifests):
        return "Library / SDK"
    if MOBILE_RE.search(manifests) or "pubspec.yaml" in top or any(
            p.endswith("AndroidManifest.xml") or ".xcodeproj/" in p for p in paths):
        return "Mobile app"
    nb = sum(1 for p in paths if p.endswith(".ipynb"))
    if DATA_RE.search(manifests) or nb >= 3:
        return "Data / ETL / ML"
    if SERVER_RE.search(manifests):
        return "Production backend"
    if FRONT_RE.search(manifests):
        return "Production frontend"
    if SCRATCH_RE.search(name) and vol["logical_loc"] < 20_000:
        return "Scratch / POC"
    if has_lib:
        return "Library / SDK"
    if vol["logical_loc"] < 3_000:
        return "Scripts / tooling"
    if any(re.search(r"(^|/)Dockerfile", p) for p in paths):
        return "Production backend"
    return "Scripts / tooling"


def readme_description(repo: Path, files: list[str]) -> str:
    readme = next((f for f in files if "/" not in f and f.lower().startswith("readme")), None)
    if not readme:
        return ""
    try:
        text = (repo / readme).read_text(encoding="utf-8", errors="ignore")[:20_000]
    except OSError:
        return ""
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if not p or p.startswith(("#", "![", "[![", "<", "|", "```", "---", "===", "-", "*")):
            continue
        p = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", p)
        p = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", p)
        p = re.sub(r"[*_`>#]", "", p)
        p = " ".join(p.split())
        if len(p) >= 20:
            return (p[:297] + "...") if len(p) > 300 else p
    return ""
