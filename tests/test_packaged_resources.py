from importlib.resources import files

from tradingagents.delivery.render import _env as delivery_templates
from tradingagents.persistence.db import _load_migrations
from tradingagents.personas.resolver import load_packaged_persona
from tradingagents.secretary.service import _env as secretary_templates


def test_packaged_text_and_yaml_resources_are_readable():
    assert "CREATE TABLE" in _load_migrations()[0].sql
    persona = load_packaged_persona("balanced")
    assert persona is not None
    assert persona.id == "balanced"
    assert load_packaged_persona("does-not-exist") is None
    assert files("tradingagents.sensing").joinpath(
        "data", "crypto_universe.yaml"
    ).is_file()
    assert files("cli").joinpath("static", "welcome.txt").is_file()


def test_packaged_jinja_templates_are_discoverable():
    secretary_templates.get_template("deep_dive.j2")
    secretary_templates.get_template("event_alert.j2")

    expected = {
        "cli/deep_dive.j2",
        "cli/event_alert.j2",
        "cli/event_alert_light.j2",
        "cli/morning_digest.j2",
        "email/deep_dive.j2",
        "email/event_alert_light.j2",
        "email/morning_digest.j2",
        "telegram/deep_dive.j2",
        "telegram/event_alert.j2",
        "telegram/event_alert_light.j2",
        "telegram/morning_digest.j2",
    }
    for template in expected:
        delivery_templates.get_template(template)
