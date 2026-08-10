# Examples

Start here. `smoke_loop.py` is a fake fine-tune with no GPU and no ML dependencies, so it proves
the whole path — image pushed, data mounted, run recorded, curves in Grafana — before you spend an
hour finding out the last step is broken.

```sh
sparks submit --context ./examples --data ./examples/data \
  --name smoke -- python /app/smoke_loop.py
```

That builds the `Dockerfile` here, which installs sparks and copies the scripts to `/app`. The
command names the script by absolute path because the container's working directory is the box's
shared directory, not the image's. Your own project builds its own image; this one exists so the
smoke run needs nothing from you.

`lora_finetune.py` is a hand-written loop with no `Trainer`: one learning-rate series per
parameter group through `log_group`, and an emitter built by hand when `from_env` returns `None`,
so the same script runs off the box.

`hf_trainer_callback.py` bridges `sparks.emit` to a HuggingFace `Trainer` in about twenty lines.
It maps Trainer's log keys to declared metric names explicitly, because those keys vary by version
and by task and an undeclared name raises.

The last two are skeletons — the model, corpus and optimiser are yours.
