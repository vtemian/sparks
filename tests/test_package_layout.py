def test_client_lives_under_sparks_client() -> None:
    from sparks.client import cli, remote

    assert callable(cli.main)
    assert hasattr(remote, "submit_remote")
