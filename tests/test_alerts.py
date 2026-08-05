"""The alert file is inert today, but a typo in it is still a latent bug.

Nothing else validates alerts/*.yml: it is not in any Prometheus config and
`make deploy` does not ship it, so a rule querying a metric nobody emits, or an
expression promtool rejects, would sit here undetected until the day someone
wires it up. This applies the dashboard checker's rule to the rules file.
"""

import re
import shutil
import subprocess
from pathlib import Path

from sparks.metrics import METRICS
from tests.check_dashboard import metric_names

ROOT = Path(__file__).resolve().parents[1]
ALERTS = sorted((ROOT / "alerts").glob("*.yml"))

# node_exporter and Prometheus internals the rules legitimately query, none of
# which sparks emits. `promhttp_` is Prometheus's own handler metrics.
ALERT_PREFIXES = ("node_", "up", "scrape_", "promhttp_")

EXPR = re.compile(r"^\s*expr:\s*(.+?)\s*$", re.MULTILINE)
PROMTOOL = shutil.which("promtool")


def exprs() -> list[str]:
    out: list[str] = []
    for path in ALERTS:
        out += EXPR.findall(path.read_text())
    return out


def allowed(name: str) -> bool:
    return name in METRICS or name.startswith(ALERT_PREFIXES)


def test_there_is_at_least_one_rule_to_check() -> None:
    assert exprs(), "no alert expressions found"


def test_every_alert_queries_a_metric_something_emits_or_scrapes() -> None:
    for expr in exprs():
        for name in metric_names(expr):
            assert allowed(name), f"{name!r} in alert {expr!r} is emitted by nobody"


def test_promtool_accepts_the_rules() -> None:
    if PROMTOOL is None:
        import pytest

        pytest.skip("promtool is not installed")
    for path in ALERTS:
        done = subprocess.run(
            [PROMTOOL, "check", "rules", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
