"""The 8.3 DDL blocks of the spec mirror store/schema.sql exactly (8.3, L-OUT-01).

schema.sql is a template whose taxonomy lists render from taxonomy.yaml; 8.3 prints the same lists as literals.
Both sides are split into statements, comments are dropped, whitespace is normalised and every list of quoted
literals is compared as a set (8.3 may print a list in another order). Every CREATE statement of 8.3 must equal the
rendered schema's statement of the same name and the reverse. The outcome_reasons seed of 8.3 must equal
`store.seed_rows` of taxonomy.yaml.
"""
import copy
import re
from pathlib import Path

from sentinel.store.store import render_schema, seed_rows
from sentinel.taxonomy import Taxonomy, load

SPEC = Path(__file__).resolve().parents[2] / "sentinel-spec" / "sections" / "08-storage-and-data-model.md"
_LITERAL_LIST = re.compile(r"\(('(?:[^']|'')*'(?:,'(?:[^']|'')*')*)\)")
_LITERAL = re.compile(r"'(?:[^']|'')*'")
_KEY = re.compile(r"^CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER|VIEW) (\w+)")
_SEED_ROW = re.compile(r"\(('(?:[^']|'')*'),('(?:[^']|'')*'),([01]),(NULL|'(?:[^']|'')*'),('(?:[^']|'')*')\)")
_INHERIT = re.compile(r"SELECT '(\w+)',reason,parametric,detail_means,'([\d.]+)' FROM outcome_reasons "
                      r"WHERE outcome = '(\w+)'")


def statements(sql: str, *, keep_comments: bool = False, spans: bool = False) -> list:
    """Split SQL into statements at `;` outside quotes and comments; a trigger runs to its `END;`.

    Returns bodies; with `keep_comments`, (leading comment lines, body) pairs whose body keeps its inline comments
    and a trailing same-line comment; with `spans` as well, (lead, body, start, end) offsets of the body."""
    out, i, n = [], 0, len(sql)
    start = None                                  # offset of the current statement's first token
    lead_from = 0
    while i < n:
        c = sql[i]
        if c == "'":
            j = i + 1
            while j < n and not (sql[j] == "'" and (j + 1 >= n or sql[j + 1] != "'")):
                j += 2 if sql[j] == "'" else 1
            i = j + 1
            continue
        if sql.startswith("--", i):
            i = sql.find("\n", i)
            i = n if i < 0 else i
            continue
        if start is None and not c.isspace():
            start = i
        if c == ";" and start is not None:
            body = sql[start:i + 1]
            flat = strip_comments(body)
            if re.match(r"\s*CREATE\s+TRIGGER", flat, re.I) and not re.search(r"\bEND\s*;\s*$", flat, re.I):
                i += 1
                continue
            end = i + 1
            m = re.match(r"[ \t]*--[^\n]*", sql[end:])
            if m:
                end += m.end()
            lead = sql[lead_from:start]
            body = sql[start:end]
            out.append((lead, body, start, end) if spans else (lead, body) if keep_comments else body)
            start, lead_from, i = None, end, end
            continue
        i += 1
    return out


def strip_comments(sql: str) -> str:
    """Drop `--` comments outside quoted literals."""
    res, i, n, quoted = [], 0, len(sql), False
    while i < n:
        c = sql[i]
        if c == "'":
            quoted = not quoted
        elif not quoted and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        res.append(c)
        i += 1
    return "".join(res)


def normalize(stmt: str) -> str:
    """Comment-free, whitespace-normalised text with every quoted-literal list sorted."""
    s = re.sub(r"\s+", " ", strip_comments(stmt)).strip()
    s = re.sub(r"\s*([(),;])\s*", r"\1", s)
    return _LITERAL_LIST.sub(lambda m: "(" + ",".join(sorted(_LITERAL.findall(m.group(1)))) + ")", s)


def key_of(stmt: str) -> str | None:
    m = _KEY.match(normalize(stmt))
    return f"{m.group(1)} {m.group(2)}" if m else None


def spec_sql() -> list[str]:
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("### 8.3 Schema"):text.index("### 8.4")]
    return re.findall(r"```sql\n(.*?)```", section, re.S)


def creates(blocks: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for b in blocks:
        for s in statements(b):
            k = key_of(s)
            if k:
                assert k not in out, f"{k} appears twice"
                out[k] = normalize(s)
    return out


def mirror_differences(spec_blocks: list[str], rendered: str) -> list[str]:
    spec, code = creates(spec_blocks), creates([rendered])
    diffs = [f"only in 8.3: {k}" for k in sorted(set(spec) - set(code))]
    diffs += [f"only in schema.sql: {k}" for k in sorted(set(code) - set(spec))]
    diffs += [f"differs: {k}\n  8.3:        {spec[k]}\n  schema.sql: {code[k]}"
              for k in sorted(set(spec) & set(code)) if spec[k] != code[k]]
    return diffs


def _lit(v: str):
    return None if v == "NULL" else int(v) if v in ("0", "1") else v[1:-1].replace("''", "'")


def spec_seed(blocks: list[str]) -> set[tuple]:
    """The 8.3 outcome_reasons seed: the VALUES rows plus the INSERT ... SELECT inheritance rows."""
    rows: dict[tuple, tuple] = {}
    for b in blocks:
        for s in statements(b):
            n = normalize(s)
            if n.startswith("INSERT INTO outcome_reasons") and "VALUES" in n:
                for m in _SEED_ROW.finditer(n):
                    r = tuple(_lit(v) for v in m.groups())
                    rows.setdefault((r[0], r[1]), r)
            elif n.startswith("INSERT OR IGNORE INTO outcome_reasons"):
                child, owner, parent = _INHERIT.search(n).groups()
                for (o, code), r in list(rows.items()):
                    if o == parent:
                        rows.setdefault((child, code), (child, code, r[2], r[3], owner))
    return set(rows.values())


# -- the parser ---------------------------------------------------------------------------------------------------

def test_parser_keeps_triggers_whole_and_ignores_quoted_delimiters():
    sql = ("CREATE TABLE t (a TEXT CHECK (a IN ('x;y','--z')));   -- trailing; comment\n"
           "-- lead\nCREATE TRIGGER g BEFORE INSERT ON t BEGIN SELECT RAISE(ABORT, 'no; way'); END;\n")
    parts = statements(sql, keep_comments=True)
    assert [key_of(b) for _, b in parts] == ["TABLE t", "TRIGGER g"]
    assert parts[0][1].endswith("-- trailing; comment") and parts[1][0].strip() == "-- lead"
    assert normalize("CHECK (k IN ('b',\n  'a'))") == normalize("CHECK (k IN ('a','b'))   -- c")


# -- the mirror ---------------------------------------------------------------------------------------------------

def test_8_3_ddl_mirrors_schema_sql():
    diffs = mirror_differences(spec_sql(), render_schema(load()))
    assert not diffs, "\n".join(diffs)


def test_the_mirror_check_sees_a_taxonomy_change():
    data = copy.deepcopy(load().data)
    data["finding_kinds"].append("test_only_kind")
    assert any(d.startswith("differs: TABLE findings") for d in mirror_differences(spec_sql(),
                                                                                   render_schema(Taxonomy(data))))


def test_8_3_seed_mirrors_the_taxonomy_seed():
    """08-seed-metadata: the (outcome, reason, parametric) rows always; owner_section and detail_means too once
    taxonomy.yaml carries `reason_metadata`. SD-54's aborted_harness bounds_source_unavailable is in 8.3 already and
    waits for the regenerated taxonomy.yaml."""
    t = load()
    spec = spec_seed(spec_sql())
    code = set(seed_rows(t))
    pending = set()
    if "bounds_source_unavailable" not in t.data["reasons"]["aborted_harness"]:
        pending = {r for r in spec if r[:2] == ("aborted_harness", "bounds_source_unavailable")}
    if t.data.get("reason_metadata"):
        assert spec - pending == code
    else:
        assert {r[:3] for r in spec - pending} == {r[:3] for r in code}
