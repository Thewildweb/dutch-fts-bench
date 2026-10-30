"""Engine-neutral query syntax, parsed once and rendered per engine.

Syntax (websearch-like): terms are ANDed, "quoted phrases", `a OR b`, `-term` excludes.
Example: `"zorg en welzijn" verpleegkundige OR verzorgende -vrijwilliger`
"""
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Clause:
    text: str
    phrase: bool = False


@dataclass
class Query:
    must: list[list[Clause]] = field(default_factory=list)  # AND of OR-groups
    must_not: list[Clause] = field(default_factory=list)

    def terms(self) -> list[str]:
        return [c.text for group in self.must for c in group]


_TOKEN = re.compile(r'(-?)"([^"]+)"|(\S+)')


def parse(text: str) -> Query:
    q = Query()
    pending_or = False
    for neg, phrase, word in _TOKEN.findall(text):
        if word == "OR":
            pending_or = bool(q.must)
            continue
        if word.startswith("-") and len(word) > 1:
            neg, word = "-", word[1:]
        clause = Clause(phrase or word, phrase=bool(phrase))
        if neg:
            q.must_not.append(clause)
        elif pending_or:
            q.must[-1].append(clause)
        else:
            q.must.append([clause])
        pending_or = False
    return q


def to_tsquery_sql(q: Query, config: str) -> tuple[str, list[str]]:
    """Postgres tsquery expression for one text-search config, with explicit grouping.

    websearch_to_tsquery can't express (a OR b) AND c, so clauses are combined with
    the tsquery operators instead."""
    args: list[str] = []

    def lit(c: Clause) -> str:
        args.append(c.text)
        fn = "phraseto_tsquery" if c.phrase else "plainto_tsquery"
        return f"{fn}('{config}', %s)"
    parts = ["(" + " || ".join(lit(c) for c in group) + ")" for group in q.must]
    parts += [f"!!{lit(c)}" for c in q.must_not]
    return "(" + " && ".join(parts) + ")", args


def to_websearch(q: Query) -> str:
    """Postgres websearch_to_tsquery syntax (no grouping: a OR b c means a OR (b AND c))."""
    def lit(c: Clause) -> str:
        return f'"{c.text}"' if c.phrase else c.text
    parts = [" or ".join(lit(c) for c in group) for group in q.must]
    parts += [f"-{lit(c)}" for c in q.must_not]
    return " ".join(parts)


def to_tantivy(q: Query, fields: list[str]) -> str:
    """Tantivy query-parser syntax (ParadeDB pg_search), searched over `fields` (`name^boost` allowed)."""
    def lit(c: Clause) -> str:
        v = c.text.replace('"', "")
        v = f'"{v}"' if c.phrase else re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", v)
        out = []
        for f in fields:
            name, _, boost = f.partition("^")
            out.append(f"{name}:{v}" + (f"^{boost}" if boost else ""))
        return "(" + " OR ".join(out) + ")"
    parts = ["(" + " OR ".join(lit(c) for c in group) + ")" for group in q.must]
    parts += [f"-{lit(c)}" for c in q.must_not]
    return " AND ".join(parts)
