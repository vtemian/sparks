"""Box server: the queue daemon (fire) and private job supervision.

Where a job goes, in order, because no single call site shows the whole chain
and one link is a module path in a dataclass default that grep will not follow:

    cli.serve            fire serve, the daemon entry point
      runner.Runner.serve    poll the spool, one job at a time
        engine.Docker.pull   fetch the image, or fail the job here
        engine.Docker.start  spawn the supervisor as the submitting user
          supervise.main         reads /etc/sparks/box.toml, refuses without it
            launch.launch          baseline, child, record, index
              process.Supervisor.run   signals, output, terminal status
                contain.main             the training container itself

`ctl` is the other way in: `fire-ctl <verb>` over SSH from a laptop, for queue,
cancel, abort, retry and remove. It talks to the same spool the runner drains.

Supervision is nested rather than in-image on purpose: a project's training
image does not have to contain sparks. engine.py's docstring argues that one.
"""
