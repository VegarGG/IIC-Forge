import pytest


@pytest.mark.unit
def test_dashboard_password_requires_long_secret(tmp_path):
    from tradingagents.dashboard.auth import load_password, password_matches

    secret = tmp_path / "password"
    secret.write_text("short", encoding="utf-8")
    with pytest.raises(RuntimeError, match="at least 20"):
        load_password(secret)
    secret.write_text("correct-horse-battery-staple", encoding="utf-8")
    expected = load_password(secret)
    assert password_matches("correct-horse-battery-staple", expected)
    assert not password_matches("wrong", expected)


@pytest.mark.unit
def test_dashboard_content_path_blocks_escape_and_symlink(tmp_path):
    from tradingagents.dashboard.auth import safe_content_path

    root = tmp_path / "data"
    root.mkdir()
    allowed = root / "briefs" / "ok.md"
    allowed.parent.mkdir()
    allowed.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = root / "briefs" / "link.md"
    link.symlink_to(outside)

    assert safe_content_path(root, "briefs/ok.md") == allowed.resolve()
    assert safe_content_path(root, "../outside.md") is None
    assert safe_content_path(root, str(outside)) is None
    assert safe_content_path(root, "briefs/link.md") is None
