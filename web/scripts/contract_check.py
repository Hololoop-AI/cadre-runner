#!/usr/bin/env python3
"""Check JSON against web/contracts/statusd.openapi.json, stdlib only.

A small JSON Schema validator for the keywords the contract uses (type,
properties, required, additionalProperties, items, prefixItems, minItems,
maxItems, enum, $ref). Any other keyword is refused rather than ignored, so
the contract cannot say something this check does not enforce.

    python3 web/scripts/contract_check.py http://127.0.0.1:8183

checks a running statusd's GET JSON endpoints against the contract; the test
suite (tests/test_web_contract.py) checks real handler answers.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONTRACT = Path(__file__).resolve().parent.parent / "contracts" / "statusd.openapi.json"
ENFORCED = {"type", "properties", "required", "additionalProperties", "items", "prefixItems",
            "minItems", "maxItems", "enum", "$ref"}
ANNOTATIONS = {"description", "title", "examples", "format", "deprecated"}
TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def load(path: Path = CONTRACT) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve(contract: dict, ref: str) -> dict:
    node = contract
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def validate(value, schema: dict, contract: dict, at: str = "$") -> list[str]:
    """Every way `value` breaks `schema`, as 'path: problem' lines."""
    unknown = {k for k in schema if k not in ENFORCED | ANNOTATIONS and not k.startswith("x-")}
    if unknown:
        raise ValueError(f"{at}: contract uses keywords this check does not enforce: {sorted(unknown)}")
    if "$ref" in schema:
        return validate(value, resolve(contract, schema["$ref"]), contract, at)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(TYPES[t](value) for t in types):
            return [f"{at}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{at}: {value!r} not one of {schema['enum']}"]
    errs: list[str] = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errs += [f"{at}: missing required {k!r}" for k in schema.get("required", []) if k not in value]
        extra = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], contract, f"{at}.{k}")
            elif extra is False:
                errs.append(f"{at}: {k!r} is not declared (closed object: add it to the contract)")
            elif isinstance(extra, dict):
                errs += validate(v, extra, contract, f"{at}[{k!r}]")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{at}: {len(value)} items, at least {schema['minItems']} expected")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{at}: {len(value)} items, at most {schema['maxItems']} expected")
        head = schema.get("prefixItems", [])
        for i, v in enumerate(value):
            sub = head[i] if i < len(head) else schema.get("items")
            if sub is not None:
                errs += validate(v, sub, contract, f"{at}[{i}]")
    return errs


def schema_keywords_ok(contract: dict) -> list[str]:
    """Every schema in the contract that uses a keyword validate() would
    refuse, found without needing a value to validate."""
    out: list[str] = []

    def check(schema, at):
        if not isinstance(schema, dict):
            return
        unknown = {k for k in schema if k not in ENFORCED | ANNOTATIONS and not k.startswith("x-")}
        if unknown:
            out.append(f"{at}: keywords not enforced: {sorted(unknown)}")
        for k, sub in schema.get("properties", {}).items():
            check(sub, f"{at}/properties/{k}")
        for i, sub in enumerate(schema.get("prefixItems", [])):
            check(sub, f"{at}/prefixItems/{i}")
        for k in ("items", "additionalProperties"):
            check(schema.get(k), f"{at}/{k}")

    for name, schema in contract["components"]["schemas"].items():
        check(schema, f"#/components/schemas/{name}")
    for name, schema in sse_events(contract).items():
        check(schema, f"x-sse-events/{name}")
    return out


def operation(contract: dict, path: str, method: str) -> dict:
    return contract["paths"][path][method.lower()]


def response_schema(contract: dict, path: str, method: str = "get", status: str = "200",
                    media: str = "application/json") -> dict:
    resp = operation(contract, path, method)["responses"][status]
    if "$ref" in resp:
        resp = resolve(contract, resp["$ref"])
    return resp["content"][media]["schema"]


def sse_events(contract: dict, path: str = "/fleet/events") -> dict:
    return operation(contract, path, "get")["x-sse-events"]


def operations(contract: dict) -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, ops in contract["paths"].items() for m in ops}


def ts_interfaces(source: str) -> dict[str, dict[str, bool]]:
    """{interface: {property: optional}} for each `export interface` in a
    TypeScript file, top-level members only (nested object types and index
    signatures are skipped). Enough for payload.ts; not a TS parser."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    out: dict[str, dict[str, bool]] = {}
    for m in re.finditer(r"export\s+interface\s+(\w+)[^{]*\{", source):
        depth, i, members, line = 1, m.end(), {}, ""
        while depth:
            c = source[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            if depth == 1 and c in ";\n":
                hit = re.match(r"\s*(\w+)(\?)?\s*:", line)
                if hit:
                    members[hit.group(1)] = bool(hit.group(2))
                line = ""
            elif depth == 1 and c not in "{}":
                line += c
            i += 1
        out[m.group(1)] = members
    return out


def main(base: str) -> int:
    contract = load()
    bad = 0
    samples = {"/fs": "?" + urllib.parse.urlencode({"q": str(Path.home()) + "/"})}
    for method, path in sorted(operations(contract)):
        op = operation(contract, path, method)
        if method != "GET" or "application/json" not in op["responses"]["200"].get("content", {}):
            continue
        url = base.rstrip("/") + path + samples.get(path, "")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = json.loads(r.read())
        except Exception as e:
            print(f"FAIL {path}: {e}")
            bad += 1
            continue
        errs = validate(body, response_schema(contract, path), contract)
        print(("ok   " if not errs else "FAIL ") + path)
        for e in errs[:20]:
            print("     " + e)
        bad += bool(errs)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8183"))
