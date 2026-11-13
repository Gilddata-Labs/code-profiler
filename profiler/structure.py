"""Functions, classes/types, parser coverage, comment+docstring ratio and test
pairing. tree-sitter when installed (pip install tree-sitter-language-pack), a
built in parser otherwise (Python ast, pattern matching for other languages)."""
from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .code import _KIND, _generic_structure, _python_structure

TS_LANG = {"Python": "python", "JavaScript": "javascript", "JSX": "javascript",
           "TypeScript": "typescript", "TSX": "tsx", "Java": "java", "Kotlin": "kotlin",
           "Go": "go", "Rust": "rust", "C": "c", "C Header": "c", "C++": "cpp",
           "C++ Header": "cpp", "C#": "csharp", "Swift": "swift", "PHP": "php", "Ruby": "ruby",
           "Scala": "scala", "Dart": "dart", "Lua": "lua", "Elixir": "elixir",
           "Haskell": "haskell", "Shell": "bash", "BASH": "bash", "Objective C": "objc",
           "R": "r", "Julia": "julia", "Erlang": "erlang", "OCaml": "ocaml",
           "Clojure": "clojure", "Groovy": "groovy", "Perl": "perl", "Solidity": "solidity",
           "Zig": "zig", "Vue": "vue", "Svelte": "svelte"}
FUNC_NODES = {
    "function_definition", "function_declaration", "function_item", "method_definition",
    "method_declaration", "constructor_declaration", "arrow_function", "function_expression",
    "function", "lambda", "lambda_expression", "anonymous_function", "func_literal",
    "closure_expression", "method", "singleton_method", "local_function_statement",
    "function_literal", "lambda_literal", "anonymous_method_expression",
    "anonymous_function_creation_expression", "generator_function_declaration",
    "generator_function", "function_signature", "method_signature", "init_declaration",
    "deinit_declaration", "secondary_constructor", "fun_decl", "function_clause",
    "local_function_declaration", "function_definition_statement", "modifier_definition",
    "constructor_definition", "fallback_receive_definition",
}
TYPE_NODES = {
    "class_definition", "class_declaration", "class_specifier", "struct_specifier",
    "enum_specifier", "interface_declaration", "enum_declaration", "record_declaration",
    "annotation_type_declaration", "struct_item", "enum_item", "trait_item", "union_item",
    "trait_definition", "object_definition", "object_declaration", "protocol_declaration",
    "struct_declaration", "class", "type_spec", "type_alias_declaration", "mixin_declaration",
    "abstract_class_declaration", "contract_declaration", "interface_definition",
    "struct_definition", "enum_definition", "trait_declaration", "module",
}
COMMENT_NODES = {"comment", "line_comment", "block_comment", "documentation_comment",
                 "doc_comment", "multiline_comment"}


def _ts_parser(lang: str):
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        return None
    try:
        return get_parser(lang)
    except Exception:  # noqa: BLE001
        return None


def tree_sitter_available() -> bool:
    return _ts_parser("python") is not None


def _ts_count(text: str, parser, lang: str) -> tuple[int, int, set[int], bool]:
    src = text.encode("utf-8", "replace")
    tree = parser.parse(src)
    root = tree.root_node
    funcs = types = 0
    comment_lines: set[int] = set()
    lines = text.splitlines()
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if t in FUNC_NODES and n.is_named:
            funcs += 1
        elif t in TYPE_NODES and n.is_named:
            if t == "module" and lang != "ruby":
                pass
            elif t in ("struct_specifier", "enum_specifier", "class_specifier") and \
                    n.child_by_field_name("body") is None:
                pass
            else:
                types += 1
        if t in COMMENT_NODES:
            r0, c0 = n.start_point
            prefix = lines[r0][:c0] if r0 < len(lines) else ""
            if not prefix.strip():
                comment_lines.update(range(r0, n.end_point[0] + 1))
        elif lang == "python" and t == "expression_statement" and n.named_child_count == 1 \
                and n.named_children[0].type == "string":
            parent = n.parent
            if parent is not None and parent.named_children and parent.named_children[0].id == n.id:
                comment_lines.update(range(n.start_point[0], n.end_point[0] + 1))
        stack.extend(n.children)
    return funcs, types, comment_lines, not root.has_error


def analyze(repo: Path, files: list[dict], cfg: dict) -> dict:
    use_ts = tree_sitter_available()
    parsers: dict[str, object] = {}
    funcs = types = 0
    comment = 0
    parsed_loc = readable_loc = 0
    by_lang: dict[str, dict] = defaultdict(lambda: {"functions": 0, "classes_types": 0,
                                                     "parsed_loc": 0, "loc": 0})
    for r in files:
        if r["class"] != "production" or not r["code_loc"]:
            continue
        nonblank = r["code_loc"] + r["comment"]
        lang = r["language"]
        p = repo / r["path"]
        try:
            if p.stat().st_size > cfg["max_file_bytes"]:
                readable_loc += nonblank
                by_lang[lang]["loc"] += nonblank
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        readable_loc += nonblank
        bl = by_lang[lang]
        bl["loc"] += nonblank
        if use_ts and lang in TS_LANG:
            key = TS_LANG[lang]
            if key not in parsers:
                parsers[key] = _ts_parser(key)
            parser = parsers[key]
            if parser is not None:
                f, t, cl, ok = _ts_count(text, parser, key)
                funcs += f; types += t; bl["functions"] += f; bl["classes_types"] += t
                if ok:
                    parsed_loc += nonblank
                    bl["parsed_loc"] += nonblank
                    comment += len(cl)
                continue
        # built in fallback
        if lang == "Python":
            res = _python_structure(text)
            if res is not None:
                parsed_loc += nonblank
                bl["parsed_loc"] += nonblank
                comment += r["comment"]
                funcs += res[0]; types += res[1]
                bl["functions"] += res[0]; bl["classes_types"] += res[1]
        elif lang in _KIND:
            res = _generic_structure(text, _KIND[lang])
            funcs += res[0]; types += res[1]
            bl["functions"] += res[0]; bl["classes_types"] += res[1]
    for v in by_lang.values():
        v["coverage"] = round(v["parsed_loc"] / v["loc"], 3) if v["loc"] else 0
    pairing = test_pairing(files)
    return {
        "parser": "tree-sitter" if use_ts else "builtin (Python ast; patterns for other languages)",
        "functions": funcs,
        "classes_types": types,
        "parser_coverage": round(parsed_loc / readable_loc, 3) if readable_loc else "",
        "comment_docstring_ratio": round(comment / parsed_loc, 4) if parsed_loc else "",
        "by_language": {k: v for k, v in sorted(by_lang.items(), key=lambda kv: -kv[1]["loc"])},
        **pairing,
    }


_STRIP = re.compile(r"^(tests?_)|(_tests?|_spec|tests?|spec|it)$", re.I)


def _base(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    while "." in name:
        name = name.rsplit(".", 1)[0]
    return name


def test_pairing(files: list[dict]) -> dict:
    """Conservative one to one match by basename: Foo <-> FooTest, foo <-> test_foo."""
    prod = [r["path"] for r in files if r["class"] == "production" and r["code_loc"]]
    tests = [r["path"] for r in files if r["class"] == "test" and r["code_loc"]]
    pool: dict[str, int] = defaultdict(int)
    for t in tests:
        b = _base(t)
        nb = re.sub(r"^(tests?_)", "", b, flags=re.I)
        nb = re.sub(r"(_tests?|_spec|Tests?|Spec|IT)$", "", nb)
        pool[nb.lower()] += 1
    matched = 0
    for p in prod:
        b = _base(p).lower()
        if pool.get(b, 0) > 0:
            pool[b] -= 1
            matched += 1
    return {"source_files": len(prod), "test_files": len(tests),
            "matched_source_files": matched,
            "untested_file_share": round(1 - matched / len(prod), 4) if prod else ""}
