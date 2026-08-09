from unittest.mock import MagicMock, patch
import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_morning_digest_now_invokes_compose_and_queues(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_IIC_DB_PATH", str(tmp_path / "iic.db"))
    monkeypatch.setenv("TRADINGAGENTS_IIC_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import tradingagents.default_config as dc
    importlib.reload(dc)

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.upsert_watchlist(conn, ticker="AAPL", ttl_until=None, tags=["user"])

    with patch("cli.morning._build_secretary") as builder:
        sec = MagicMock()
        sec.compose_morning_digest.return_value = "br1"
        builder.return_value = (sec, conn)

        (tmp_path / "data" / "briefs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "briefs" / "br1.md").write_text("BODY")
        # Insert the rows the real compose call would atomically enqueue.
        store.insert_brief(
            conn, brief_id="br1", mode="morning_digest", scope='["AAPL"]',
            generated_ts="2026-05-27T07:00:00+00:00",
            content_path="briefs/br1.md", run_ids=["r1"],
        )
        conn.execute(
            "INSERT INTO delivery_queue "
            "(idempotency_key, brief_id, channel, mode, brief_payload, body, "
            "state, attempt_count, max_attempts, available_ts, created_ts, updated_ts) "
            "VALUES ('k1', 'br1', 'telegram', 'morning_digest', '{}', 'body', "
            "'queued', 0, 5, '2026-05-27T07:00:00+00:00', "
            "'2026-05-27T07:00:00+00:00', '2026-05-27T07:00:00+00:00')"
        )
        conn.commit()

        from cli.morning import morning_digest_now
        morning_digest_now(dry_run=False)

    kwargs = sec.compose_morning_digest.call_args.kwargs
    assert kwargs["watchlist"] is None
    assert kwargs["deliver"] is True
    assert "queued 1 durable delivery job(s)" in capsys.readouterr().out


@pytest.mark.unit
def test_morning_digest_dry_run_skips_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_IIC_DB_PATH", str(tmp_path / "iic.db"))
    monkeypatch.setenv("TRADINGAGENTS_IIC_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import tradingagents.default_config as dc
    importlib.reload(dc)

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.upsert_watchlist(conn, ticker="AAPL", ttl_until=None, tags=["user"])

    with patch("cli.morning._build_secretary") as builder:
        sec = MagicMock()
        sec.compose_morning_digest.return_value = "br1"
        builder.return_value = (sec, conn)

        (tmp_path / "data" / "briefs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "briefs" / "br1.md").write_text("BODY")

        from cli.morning import morning_digest_now
        morning_digest_now(dry_run=True)

    assert sec.compose_morning_digest.call_args.kwargs["deliver"] is False


@pytest.mark.unit
def test_digest_tail_prints_latest(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_IIC_DB_PATH", str(tmp_path / "iic.db"))
    monkeypatch.setenv("TRADINGAGENTS_IIC_DATA_DIR", str(tmp_path / "data"))
    import importlib
    import tradingagents.default_config as dc
    importlib.reload(dc)

    conn = iic_connect(str(tmp_path / "iic.db"))
    (tmp_path / "data" / "briefs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "briefs" / "br1.md").write_text("LATEST DIGEST")

    store.insert_brief(
        conn, brief_id="br1", mode="morning_digest", scope='["AAPL"]',
        generated_ts="2026-05-27T07:00:00+00:00",
        content_path="briefs/br1.md", run_ids=["r1"],
    )

    from cli.morning import digest_tail
    digest_tail()
    captured = capsys.readouterr()
    assert "LATEST DIGEST" in captured.out
