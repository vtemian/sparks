"""Every panel query names a metric something actually emits.

sparkup's equivalent cannot check this board: it hardcodes one file path,
asserts the uid matches the Grafana home page, derives its allowlist only from
node_exporter's collectors, and refuses any expression containing a Grafana
variable. This is the same rule applied to a pushed dashboard.

    uv run python tests/check_dashboard.py
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sparks.metrics import METRICS

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "dashboards"

# Scraped by sparkup, so legitimate here even though sparks never emits them.
# Prefixes, because node_exporter names a series after every key in
# /proc/meminfo and an exhaustive list would be a claim about a kernel.
SCRAPED_PREFIXES = ("node_", "nvidia_smi_", "up", "scrape_")

# What a Grafana variable becomes for parsing purposes. A real run id, so the
# regex the datasource would generate is the regex promtool parses.
VARIABLES = {
    "$run_id": "run-20260804-1530-demo",
    "$__rate_interval": "5m",
    "$__interval": "1m",
    "$__range": "3h",
    # A Grafana constant variable, so the tariff can change without a dashboard
    # rebuild. Substituted here as a plain number so the expression parses.
    "$tariff": "1.3",
}

# Strip the parts of an expression that contain identifiers which are not
# metric names, then read what is left. A single "identifier followed by
# something" regex is not enough: it misses the metric inside
# `max by (...) (training_run_info)`, which is the shape every joined panel
# uses, and a checker that silently skips the interesting half is worse than
# no checker.
STRINGS = re.compile(r"\"[^\"]*\"|'[^']*'")
LABELS = re.compile(r"\{[^}]*\}")
RANGES = re.compile(r"\[[^\]]*\]")
MODIFIERS = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)"
)
LABEL_VALUES = re.compile(r"label_values\(\s*(.+?)\s*,\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)")
NAME_MATCHER = re.compile(r"__name__\s*=~?\s*\"([^\"]*)\"")
IDENT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
CALL = re.compile(r"([a-zA-Z_:][a-zA-Z0-9_:]*)\s*\(")
KEYWORDS = {
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "offset",
    "and",
    "or",
    "unless",
    "bool",
    "default",
    "start",
    "end",
}


class CheckFailed(Exception):
    """A panel query this dashboard should not ship with."""


def substitute(expr: str) -> str:
    for name, value in VARIABLES.items():
        expr = expr.replace(name, value)
    if "$" in expr:
        raise CheckFailed(f"unknown Grafana variable in {expr!r}")
    return expr


def metric_names(expr: str) -> set[str]:
    """Every identifier left once the non-metric ones are removed.

    Verified against every expression the shipped dashboard contains, including
    `... group_left(run_name, git_sha) max by (run_id, ...) (training_run_info)`,
    where the metric sits inside a parenthesised group that a naive scan skips.
    """
    # `{__name__="foo"}` names a metric from inside the label set, so it has to
    # be read out before the label set is stripped. Otherwise the whole
    # expression reduces to nothing and a panel querying an invented metric
    # passes with an empty result set.
    named = set(NAME_MATCHER.findall(expr))
    stripped = STRINGS.sub(" ", expr)
    stripped = LABELS.sub(" ", stripped)
    stripped = RANGES.sub(" ", stripped)
    stripped = MODIFIERS.sub(" ", stripped)
    called = set(CALL.findall(stripped))  # functions and aggregators
    return named | {
        name
        for name in IDENT.findall(stripped)
        if name not in called and name not in KEYWORDS
    }


def allowed(name: str) -> bool:
    return name in METRICS or name.startswith(SCRAPED_PREFIXES)


def walk_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Every panel, including the children of a collapsed row.

    Grafana moves a row's children *inside* that row's own `panels` array when
    the row is collapsed, so a one-level walk stops seeing them the moment
    somebody collapses a row in the UI and re-exports. That is a silent loss of
    coverage rather than a failure, which is the worst kind.
    """
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def expressions(dashboard: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for panel in walk_panels(dashboard["panels"]):
        for target in panel.get("targets", []):
            if "expr" in target:
                out.append(target["expr"])
    # An annotation naming a metric nobody emits draws no region and fails
    # nothing else. Grafana's own built-in annotations are not PromQL, so they
    # are skipped by looking at the datasource rather than the shape.
    for anno in dashboard.get("annotations", {}).get("list", []):
        if anno.get("datasource", {}).get("type") != "prometheus":
            continue
        expr = anno.get("target", {}).get("expr")
        if expr:
            out.append(expr)
    # The variable query is the dashboard's entry point: a typo there ships an
    # empty Run dropdown and every panel is blank, with nothing else failing.
    for variable in dashboard.get("templating", {}).get("list", []):
        definition = variable.get("definition")
        if definition:
            out.append(LABEL_VALUES.sub(r"\1", definition))
    return out


def dashboards() -> list[Path]:
    """Every board this repo ships. A hardcoded single path is how a second
    dashboard ends up shipping unvalidated, which is the gap in sparkup's
    equivalent."""
    return sorted(DASHBOARDS.glob("*.json"))


def check(extra_exprs: list[str] | None = None) -> None:
    boards = dashboards()
    if not boards:
        raise CheckFailed("no dashboards found to check")
    exprs = list(extra_exprs or [])
    for path in boards:
        found = expressions(json.loads(path.read_text()))
        if not found:
            raise CheckFailed(f"{path.name} has no queries at all")
        exprs += found
    for expr in exprs:
        for name in metric_names(substitute(expr)):
            if not allowed(name):
                raise CheckFailed(
                    f"{name!r} is neither declared in sparks.metrics.METRICS "
                    f"nor scraped by sparkup: {expr!r}"
                )


def main() -> int:
    try:
        check()
    except CheckFailed as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("ok: " + ", ".join(p.name for p in dashboards()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
