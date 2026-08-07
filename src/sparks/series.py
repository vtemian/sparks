"""A metric name plus its labels, in the one form the rest of the code uses.

Labels are validated on construction rather than on push. Prometheus drops a
series carrying an invalid label name and still answers 200, counting it only in
`prometheus_api_remote_write_invalid_labels_samples_total`, so a typo produces a
green push and no data. This is the only place that mistake is visible.
"""

import re
from dataclasses import dataclass

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class InvalidLabelError(ValueError):
    """A metric or label name Prometheus would refuse or silently drop."""


@dataclass(frozen=True)
class Series:
    """One time series: a metric name and a sorted, hashable label set."""

    name: str
    labels: tuple[tuple[str, str], ...]

    def __init__(self, name: str, labels: dict[str, str]) -> None:
        if not NAME.match(name):
            raise InvalidLabelError(f"{name!r} is not a valid metric name")
        for key in labels:
            if key.startswith("__"):
                raise InvalidLabelError(f"{key!r} is reserved")
            if not LABEL.match(key):
                raise InvalidLabelError(f"{key!r} is not a valid label name")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "labels", tuple(sorted(labels.items())))

    def as_metric(self) -> dict[str, str]:
        """The wire form: labels plus the name under `__name__`."""
        return {"__name__": self.name, **dict(self.labels)}
