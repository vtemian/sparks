from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import time
from pathlib import Path
from typing import Any

from sparks import box, spool, summary

LOG = logging.getLogger("sparks")


class ControlError(Exception): ...


HEADINGS = ("JOB", "USER", "STATE", "AGE", "RUN")

TAIL_LINES = 200


def fetch_contract() -> box.Box:
    provisioned = box.load()
    if provisioned is None:
        raise ControlError(
            f"{box.config_path()} does not exist; this box has no sparks contract"
        )

    return provisioned


def queue_dir(shared_dir: Path | None = None) -> Path:
    if shared_dir is not None:
        return Path(shared_dir) / "queue"

    directory = fetch_contract().queue_dir
    if not directory.is_dir():
        raise ControlError(
            f"this box is provisioned for sparks but not for the queue: "
            f"{directory} does not exist"
        )
    return directory


def runs_dir(shared_dir: Path | None = None) -> Path:
    if shared_dir is not None:
        return Path(shared_dir) / "runs"

    return fetch_contract().runs_dir


def retry(queue_dir: Path, entry: spool.Entry) -> spool.Entry:
    if not entry.is_terminal:
        raise ControlError(
            f"{entry.job.job_id} is {entry.state.state}. Retrying a job that has "
            "not finished would run the same thing twice at once"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ControlError(
            f"{entry.job.job_id} belongs to {entry.job.user}, and only they "
            "can retry it (retry clones their data directory)"
        )

    # Before anything is reserved, so a refusal leaves no half-built job.
    carried = carried_secrets(entry)
    who = entry.job.user
    job_id, path = spool.reserve(queue_dir, entry.job.name, who)
    source = entry.data_dir
    if source.is_dir():
        # Hardlink where the filesystem allows it. Safe only because nothing
        # ever writes into a committed job's `data/` after submit.
        destination = path / spool.DATA_DIR
        try:
            shutil.copytree(source, destination, copy_function=os.link)
        except OSError as exc:
            LOG.info("sparks: could not hardlink data (%s); copying instead", exc)
            shutil.copytree(source, destination, dirs_exist_ok=True)
    if carried is not None:
        # Copied rather than hardlinked: the retry belongs to whoever asked
        # for it, and a hardlink would give both jobs one 0600 file.
        spool.write_env(path, carried)

    return spool.commit(
        path,
        spool.Job(
            job_id=job_id,
            name=entry.job.name,
            user=who,
            command=list(entry.job.command),
            submitted_unix=time.time(),
            git_sha=entry.job.git_sha,
            git_dirty=entry.job.git_dirty,
            retry_of=entry.job.job_id,
            image=entry.job.image,
            env=dict(entry.job.env),
            secret_names=list(entry.job.secret_names),
        ),
    )


def carried_secrets(entry: spool.Entry) -> dict[str, str] | None:
    if entry.env_file.is_file():
        return spool.read_env(entry.env_file)

    if not entry.job.secret_names:
        return None

    named = ", ".join(entry.job.secret_names)
    raise ControlError(
        f"{entry.job.job_id} was submitted with {named}, and {entry.env_file} "
        "is gone, so the retry would start without it. Submit the job again "
        f"with --secret {entry.job.secret_names[0]}"
    )


def resolve(queue_dir: Path, needle: str) -> spool.Entry:
    found = spool.entries(queue_dir)
    if not found:
        raise ControlError("there are no jobs in the queue")

    exact = [entry for entry in found if entry.job.job_id == needle]
    if exact:
        return exact[0]

    matches = [
        entry
        for entry in found
        if needle in entry.job.job_id or entry.job.name == needle
    ]
    if not matches:
        raise ControlError(f"no job matches {needle!r}")
    return disambiguate(matches, needle)


def disambiguate(matches: list[spool.Entry], needle: str) -> spool.Entry:
    if len(matches) == 1:
        return matches[0]

    live = [entry for entry in matches if not entry.is_terminal]
    # One running job among six finished ones is not ambiguous.
    if len(live) == 1:
        return live[0]

    ids = "\n".join(f"    {entry.job.job_id}  {entry.state.state}" for entry in matches)
    raise ControlError(f"{needle!r} matches several jobs:\n{ids}")


def ask(queue_dir: Path, needle: str, action: str) -> spool.Entry:
    entry = resolve(queue_dir, needle)
    if entry.is_terminal:
        raise ControlError(
            f"{entry.job.job_id} has already {entry.state.state}; there is "
            "nothing to stop"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ControlError(
            f"{entry.job.job_id} belongs to {entry.job.user}, and only they can stop it"
        )

    spool.request(entry.path, action)
    return entry


def remove(queue_dir: Path, needle: str) -> spool.Entry:
    entry = resolve(queue_dir, needle)
    if not entry.is_terminal:
        raise ControlError(
            f"{entry.job.job_id} is {entry.state.state}. Stop it first: "
            f"sparks abort {entry.job.job_id}"
        )
    if not entry.may_be_controlled_by(os.getuid()):
        raise ControlError(f"{entry.job.job_id} belongs to {entry.job.user}")

    spool.remove(entry.path)
    return entry


def run_dir(entry: spool.Entry, shared_dir: Path | None = None) -> Path:
    if entry.state.run_id is None:
        raise ControlError(nothing_ran_yet(entry))

    return runs_dir(shared_dir) / entry.state.run_id


def nothing_ran_yet(entry: spool.Entry) -> str:
    said = f"{entry.job.job_id} is {entry.state.state} and has no run directory yet"
    if entry.state.detail:
        return f"{said}: {entry.state.detail}"

    return f"{said}. sparks status {entry.job.job_id} says where it is"


def logs(
    entry: spool.Entry,
    shared_dir: Path | None = None,
    tail: int | None = TAIL_LINES,
) -> str:
    directory = run_dir(entry, shared_dir)
    written = tail_of(directory / summary.OUTPUT_LOG, tail)
    # Labelled, not concatenated: what the container printed and why sparks
    # could not run it are different claims, and a run that died on exec has
    # only the second.
    failed = read_text(directory / summary.ERROR_FILE)
    if failed:
        written += f"\nsparks could not run this job:\n{failed}"
    if not written:
        raise ControlError(f"{entry.job.job_id} has written no output yet")

    return written


def tail_of(path: Path, lines: int | None) -> str:
    text = read_text(path)
    if lines is None or not text:
        return text

    return "".join(text.splitlines(keepends=True)[-lines:])


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def status(entry: spool.Entry, shared_dir: Path | None = None) -> dict[str, Any]:
    record = find_summary(entry, shared_dir)
    return {
        "job": entry.job.to_dict(),
        "state": entry.state.to_dict(),
        "summary": None if record is None else record.to_dict(),
    }


def find_summary(
    entry: spool.Entry, shared_dir: Path | None = None
) -> summary.Summary | None:
    if entry.state.run_id is None:
        return None

    path = runs_dir(shared_dir) / entry.state.run_id / summary.FILENAME
    try:
        return summary.load(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        LOG.warning("sparks: %s has an unreadable record: %s", entry.state.run_id, exc)
        return None


def as_json(entries: list[spool.Entry]) -> str:
    rows = [
        {
            "job_id": entry.job.job_id,
            "name": entry.job.name,
            "user": entry.job.user,
            "state": entry.state.state,
            "run_id": entry.state.run_id,
            "image": entry.job.image,
            "command": list(entry.job.command),
            "submitted_unix": entry.job.submitted_unix,
            "started_unix": entry.state.started_unix,
            "finished_unix": entry.state.finished_unix,
            "exit_code": entry.state.exit_code,
            "detail": entry.state.detail,
        }
        for entry in entries
    ]
    return json.dumps(rows, indent=2) + "\n"


def render_status(payload: dict[str, Any]) -> str:
    job, state = payload["job"], payload["state"]
    rows = [
        ("job", job["job_id"]),
        ("name", job["name"]),
        ("user", job["user"]),
        ("state", state["state"]),
        ("image", job["image"]),
        ("command", shlex.join(job["command"])),
        ("env", " ".join(f"{name}={value}" for name, value in job["env"].items())),
        ("secrets", " ".join(job["secret_names"])),
        ("run", state["run_id"] or ""),
        ("exit", "" if state["exit_code"] is None else str(state["exit_code"])),
        ("detail", state["detail"] or ""),
        *record_rows(payload["summary"]),
    ]
    width = max(len(label) for label, _ in rows)
    shown = [f"{label.ljust(width)}  {value}" for label, value in rows if value != ""]
    return "".join(f"{line}\n" for line in shown)


def record_rows(record: dict[str, Any] | None) -> list[tuple[str, str]]:
    if record is None:
        return []

    joules = record["energy"]["total_joules"]
    loss = record.get("final_loss")
    return [
        ("status", record["status"]),
        ("duration", duration(record["duration_seconds"])),
        ("signal", record["signal"] or ""),
        ("energy", "" if joules is None else f"{joules / 3600:.0f} Wh"),
        ("loss", "" if loss is None else f"{loss:.4f}"),
    ]


def render(entries: list[spool.Entry], now: float | None = None) -> str:
    if not entries:
        return "the queue is empty\n"

    moment = time.time() if now is None else now
    rows = [
        (
            entry.job.job_id,
            entry.job.user,
            entry.state.state,
            age(entry, moment),
            entry.state.run_id or "",
        )
        for entry in entries
    ]
    widths = [
        max(len(str(row[index])) for row in (HEADINGS, *rows))
        for index in range(len(HEADINGS))
    ]
    lines = [render_row(HEADINGS, widths), *(render_row(row, widths) for row in rows)]
    return "".join(f"{line}\n" for line in lines)


def render_row(values: tuple[str, ...], widths: list[int]) -> str:
    padded = [value.ljust(width) for value, width in zip(values, widths, strict=True)]
    line = "  ".join(padded)
    return line.rstrip()


def age(entry: spool.Entry, now: float) -> str:
    since = entry.state.started_unix or entry.job.submitted_unix
    if entry.is_terminal and entry.state.finished_unix:
        since = entry.state.finished_unix
    return duration(max(0.0, now - since))


_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR


def duration(seconds: float) -> str:
    if seconds < _MINUTE:
        return f"{int(seconds)}s"

    if seconds < _HOUR:
        return f"{int(seconds // _MINUTE)}m"

    if seconds < _DAY:
        return f"{seconds / _HOUR:.1f}h"

    return f"{int(seconds // _DAY)}d"
