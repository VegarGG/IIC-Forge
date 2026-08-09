"""Single-operator password gate backed by a Docker secret file."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path


DEFAULT_SECRET_PATH = "/run/secrets/operator_dashboard_password"


def load_password(path: str | Path | None = None) -> str:
    secret_path = Path(
        path or os.environ.get("IIC_DASHBOARD_PASSWORD_FILE", DEFAULT_SECRET_PATH)
    )
    try:
        password = secret_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("operator dashboard password secret is unavailable") from exc
    if len(password) < 20:
        raise RuntimeError("operator dashboard password must be at least 20 characters")
    return password


def password_matches(candidate: str, expected: str) -> bool:
    left = hashlib.sha256(candidate.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def require_authentication() -> None:
    """Stop Streamlit rendering until the one operator authenticates."""
    import streamlit as st

    try:
        expected = load_password()
    except RuntimeError as exc:
        st.error(str(exc))
        st.stop()
        return
    if st.session_state.get("iic_operator_authenticated") is True:
        if st.sidebar.button("Sign out"):
            st.session_state["iic_operator_authenticated"] = False
            st.rerun()
        return
    st.title("IIC-FORGE Operator")
    with st.form("operator_login", clear_on_submit=True):
        candidate = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        if password_matches(candidate, expected):
            st.session_state["iic_operator_authenticated"] = True
            st.rerun()
        st.error("Authentication failed")
    st.stop()


def safe_content_path(data_dir: str | Path, relative: str) -> Path | None:
    """Resolve a stored artifact without allowing absolute/symlink escapes."""
    root = Path(data_dir).expanduser().resolve()
    candidate = Path(relative)
    if candidate.is_absolute():
        return None
    try:
        resolved = (root / candidate).resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved
