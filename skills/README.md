# Skills

Two Claude Code skills for driving sparks. Install by linking them where Claude looks:

```sh
ln -s "$PWD"/skills/* ~/.claude/skills/
```

`authoring-a-sparks-job` covers instrumenting training code, the job Dockerfile, and
submitting. `operating-the-sparks-queue` covers watching a run, working out why it failed,
and stopping or resubmitting it.
