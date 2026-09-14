import pytest

from ingest.handler import main


def test_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAW_BUCKET", "test-bucket")

    result = main({}, None)

    assert result == {"status": "ok"}


def test_raises_without_raw_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAW_BUCKET", raising=False)

    with pytest.raises(KeyError):
        main({}, None)
