# Skills

The skills live in `src/sparks/skills/` and ship in the package. `sparks setup`
(and so `make install`) is what puts them in `~/.claude/skills` and
`~/.agents/skills`. This directory is the same two folders, linked.

`authoring-a-sparks-job` covers instrumenting training code, the job Dockerfile, and
submitting. `operating-the-sparks-queue` covers watching a run, working out why it failed,
and stopping or resubmitting it.
