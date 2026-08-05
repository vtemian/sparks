"""The alert file is inert today, but a typo in it is still a latent bug.

Nothing else validates alerts/*.yml: it is not in any Prometheus config and
`make deploy` does not ship it, so a rule querying a metric nobody emits, or an
expression promtool rejects, would sit here undetected until the day someone
wires it up. This applies the dashboard checker's rule to the rules file.
"""

import re
from pathlib import Path

import pytest

from tests import promtool
from tests.check_dashboard import allowed as dashboard_allowed
from tests.check_dashboard import metric_names

ROOT = Path(__file__).resolve().parents[1]
ALERTS = sorted((ROOT / "alerts").glob("*.yml"))

# Reuse the dashboard checker's allowlist rather than restating it: the two had
# drifted, and this copy was the looser one, so a metric the dashboards would
# have rejected passed here. The rules need exactly one prefix the dashboards
# do not, Prometheus's own handler metrics, which no panel plots.
ALERT_PREFIXES = ("promhttp_",)

EXPR = re.compile(r"^\s*expr:\s*(.+?)\s*$", re.MULTILINE)


def exprs() -> list[str]:
    out: list[str] = []
    for path in ALERTS:
        out += EXPR.findall(path.read_text())
    return out


def allowed(name: str) -> bool:
    return dashboard_allowed(name) or name.startswith(ALERT_PREFIXES)


def test_there_is_at_least_one_rule_to_check() -> None:
    assert exprs(), "no alert expressions found"


def test_every_alert_queries_a_metric_something_emits_or_scrapes() -> None:
    for expr in exprs():
        for name in metric_names(expr):
            assert allowed(name), f"{name!r} in alert {expr!r} is emitted by nobody"


@pytest.mark.skipif(not promtool.usable(), reason=promtool.REASON)
def test_promtool_accepts_the_rules() -> None:
    for path in ALERTS:
        done = promtool.check_rules(path)
        assert done.returncode == 0, done.stdout + done.stderr
