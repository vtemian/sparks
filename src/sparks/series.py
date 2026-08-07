import re
from dataclasses import dataclass

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class InvalidLabelError(ValueError): ...


@dataclass(frozen=True)
class Series:
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
        return {"__name__": self.name, **dict(self.labels)}
