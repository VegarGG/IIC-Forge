from unittest.mock import MagicMock, patch
import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


@pytest.mark.unit
def test_email_outbound_uses_smtplib_and_records_message_id(tmp_path):
    from tradingagents.delivery.email import EmailOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="deep_dive",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"email": "full"},
        },
        "smtp": {
            "enabled": True,
            "host": "smtp.gmail.com",
            "port": 587,
            "from_addr": "watter008@gmail.com",
            "to_addrs": ["watter008@gmail.com"],
        },
    }
    fake_smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=fake_smtp) as smtp_ctor, patch.dict(
        "os.environ", {"IIC_SMTP_USER": "u", "IIC_SMTP_APP_PASSWORD": "p"}
    ):
        ch = EmailOutbound(conn=conn, config=cfg)
        delivery_id = ch.send(
            brief={"brief_id": "b1", "mode": "deep_dive"},
            mode="deep_dive",
            body=(
                "<html><body><p>BODY</p><script>alert(1)</script>"
                '<a href="javascript:alert(2)" onclick="bad()">link</a>'
                "</body></html>"
            ),
        )

    smtp_ctor.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
    fake_smtp.starttls.assert_called_once()
    fake_smtp.login.assert_called_once_with("u", "p")
    fake_smtp.send_message.assert_called_once()
    sent_msg = fake_smtp.send_message.call_args[0][0]
    assert sent_msg["From"] == "watter008@gmail.com"
    assert sent_msg["To"] == "watter008@gmail.com"
    html_body = sent_msg.get_body(preferencelist=("html",)).get_content()
    assert "BODY" in html_body
    assert "script" not in html_body.lower()
    assert "javascript:" not in html_body.lower()
    assert "onclick" not in html_body.lower()

    row = conn.execute(
        "SELECT channel, status, channel_ref FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert row[0] == "email"
    assert row[1] == "sent"
    assert row[2] is not None


@pytest.mark.unit
def test_email_message_id_is_stable_across_attempts(tmp_path):
    from tradingagents.delivery.email import EmailOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {"quiet_hours": {"enabled": False}},
        "smtp": {
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "from_addr": "sender@example.com",
            "to_addrs": ["ops@example.com"],
        },
    }
    fake_smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=fake_smtp), patch.dict(
        "os.environ", {"IIC_SMTP_USER": "u", "IIC_SMTP_APP_PASSWORD": "p"}
    ):
        channel = EmailOutbound(conn=conn, config=cfg)
        first = channel.send_attempt(
            brief={"brief_id": "b1", "mode": "event_alert"},
            mode="event_alert",
            body="<p>first transport attempt</p>",
        )
        second = channel.send_attempt(
            brief={"brief_id": "b1", "mode": "event_alert"},
            mode="event_alert",
            body="<p>retry</p>",
        )

    refs = [
        row[0]
        for row in conn.execute(
            "SELECT channel_ref FROM deliveries WHERE delivery_id IN (?, ?) "
            "ORDER BY delivery_id",
            (first, second),
        )
    ]
    assert refs[0] == refs[1]
    assert refs[0].startswith("<iic-")


@pytest.mark.unit
def test_email_alert_auth_rejection_is_permanently_blocked(tmp_path):
    from tradingagents.delivery.email import EmailOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="event_alert",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {"quiet_hours": {"enabled": False}},
        "smtp": {
            "enabled": True,
            "host": "smtp.example.com",
            "port": 587,
            "from_addr": "sender@example.com",
            "to_addrs": ["ops@example.com"],
        },
    }
    fake_smtp = MagicMock()
    import smtplib

    fake_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad auth")
    with patch("smtplib.SMTP", return_value=fake_smtp), patch.dict(
        "os.environ", {"IIC_SMTP_USER": "u", "IIC_SMTP_APP_PASSWORD": "bad"}
    ):
        delivery_id = EmailOutbound(conn=conn, config=cfg).send_attempt(
            brief={"brief_id": "b1", "mode": "event_alert"},
            mode="event_alert",
            body="<p>alert</p>",
        )
    row = conn.execute(
        "SELECT status, skip_reason FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert tuple(row) == ("blocked", "credential_rejected")


@pytest.mark.unit
def test_email_outbound_disabled_records_skipped(tmp_path):
    from tradingagents.delivery.email import EmailOutbound

    conn = iic_connect(str(tmp_path / "iic.db"))
    store.insert_brief(
        conn,
        brief_id="b1",
        mode="deep_dive",
        scope="AAPL",
        generated_ts="2026-05-27T12:00:00+00:00",
        content_path="briefs/b1.md",
        run_ids=["r1"],
    )
    cfg = {
        "delivery": {
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
            "digest_modes": {"email": "full"},
        },
        "smtp": {
            "enabled": False,
            "host": "smtp.gmail.com",
            "port": 587,
            "from_addr": "x@y",
            "to_addrs": ["x@y"],
        },
    }
    ch = EmailOutbound(conn=conn, config=cfg)
    delivery_id = ch.send(
        brief={"brief_id": "b1", "mode": "deep_dive"}, mode="deep_dive", body="..."
    )
    row = conn.execute(
        "SELECT status, skip_reason FROM deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert row[0] == "skipped"
    assert row[1] == "smtp_disabled"
