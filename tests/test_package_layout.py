def test_client_lives_under_sparks_client() -> None:
    from sparks.client import cli, remote

    assert callable(cli.main)
    assert hasattr(remote, "submit_remote")


def test_fire_daemon_and_supervise_modules_exist() -> None:
    from sparks.fire import cli, supervise

    assert callable(cli.main)
    assert callable(supervise.main)


def test_only_sparks_and_fire_console_scripts() -> None:
    import importlib.metadata

    scripts = importlib.metadata.entry_points().select(group="console_scripts")
    names = {ep.name for ep in scripts if ep.value.startswith("sparks.")}
    assert "sparks" in names
    assert "fire" in names
    assert "sparks-run" not in names
    assert "sparks-runner" not in names
