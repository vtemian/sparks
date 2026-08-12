from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_queue_image_copies_the_readme_pyproject_declares() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    readme = declared["project"]["readme"]
    copied = re.search(
        r"^COPY ([^\n]*) \./$", (ROOT / "Dockerfile").read_text(encoding="utf-8"), re.M
    )

    assert copied is not None, "the Dockerfile no longer has a COPY … ./ line"
    assert readme in copied.group(1).split(), (
        f"{readme} is pyproject's readme but the image never copies it; "
        "uv sync fails at build with 'Readme file does not exist'"
    )


def test_the_declared_readme_is_a_real_file() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (ROOT / declared["project"]["readme"]).is_file()
