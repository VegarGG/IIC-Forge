"""Streamlit dashboard entrypoint.

Run via: streamlit run tradingagents/dashboard/app.py
"""

from __future__ import annotations

import os

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.db import connect as iic_connect
from tradingagents.dashboard.panels.briefs import fetch_recent_briefs, fetch_brief_thread
from tradingagents.dashboard.auth import require_authentication, safe_content_path
from tradingagents.ops.logging import configure_logging


configure_logging("dashboard")
st.set_page_config(page_title="IIC-FORGE Dashboard", layout="wide")


@st.cache_resource
def _dashboard_heartbeat():
    from tradingagents.ops.heartbeat import ServiceHeartbeat

    return ServiceHeartbeat(
        DEFAULT_CONFIG["iic_db_path"],
        "dashboard",
        interval_seconds=DEFAULT_CONFIG["operator_heartbeat_interval_seconds"],
    ).start()


_dashboard_heartbeat()
require_authentication()


@st.cache_resource
def _conn():
    return iic_connect(DEFAULT_CONFIG["iic_db_path"])


st.title("IIC-FORGE")

tab_system, tab_briefs, tab_costs, tab_queue, tab_actions = st.tabs(
    ["System", "Briefs", "Costs", "Queue", "Actions"]
)

with tab_system:
    from tradingagents.dashboard.panels.system import (
        fetch_open_alerts,
        fetch_system_status,
    )

    st.header("Production status")
    snapshot = fetch_system_status(
        _conn(),
        DEFAULT_CONFIG,
        backup_root=os.environ.get("IIC_BACKUP_DIR", "/backups"),
    )
    metrics = st.columns(4)
    analysis = snapshot["queues"]["analysis"]["counts"]
    delivery = snapshot["queues"]["delivery"]["counts"]
    metrics[0].metric("Analysis pending", analysis.get("queued", 0) + analysis.get("running", 0))
    metrics[1].metric("Delivery pending", delivery.get("queued", 0) + delivery.get("running", 0))
    metrics[2].metric("LLM budget used", f"{snapshot['budget']['utilization_percent']:.1f}%")
    metrics[3].metric("Backup", snapshot["backup"]["status"])
    st.subheader("Service heartbeats")
    st.dataframe(
        [{"service": name, **value} for name, value in snapshot["heartbeats"].items()],
        use_container_width=True,
    )
    st.subheader("Open operational alerts")
    st.dataframe(fetch_open_alerts(_conn()) or [{"info": "none"}], use_container_width=True)
    with st.expander("Machine-readable status"):
        st.json(snapshot)

with tab_briefs:
    st.header("Recent briefs")
    rows = fetch_recent_briefs(_conn(), limit=50)
    if not rows:
        st.info("No briefs yet.")
    else:
        st.dataframe(rows, use_container_width=True)
        selected = st.selectbox(
            "View brief thread",
            options=[""] + [r["brief_id"] for r in rows],
        )
        if selected:
            thread = fetch_brief_thread(_conn(), brief_id=selected)
            for b in thread:
                st.subheader(f"{b['brief_id']} (depth={b['refine_depth']})")
                body_path = safe_content_path(
                    DEFAULT_CONFIG["iic_data_dir"], b["content_path"]
                )
                if body_path is not None:
                    st.markdown(body_path.read_text(encoding="utf-8")[:2_000_000])
                else:
                    st.warning("Brief artifact is missing or outside the data directory.")

with tab_costs:
    st.header("Daily cost trend")
    from tradingagents.dashboard.panels.costs import (
        fetch_daily_cost_trend,
        fetch_provider_split,
    )

    # Provider split — local vs API call volume and API spend.
    # Post-cutover target: api_spend -> 0 for gate/triage workloads.
    split = fetch_provider_split(_conn())
    split_cols = st.columns(5)
    split_cols[0].metric("Local calls", split["local_calls"])
    split_cols[1].metric("API calls", split["api_calls"])
    split_cols[2].metric("Free calls", split["free_calls"])
    split_cols[3].metric("Unknown calls", split["unknown_calls"])
    split_cols[4].metric("API spend ($)", f"{split['api_spend']:.4f}")

    st.divider()

    rows = fetch_daily_cost_trend(_conn(), days=30)
    if not rows:
        st.info("No cost data yet.")
    else:
        import altair as alt
        import pandas as pd
        df = pd.DataFrame(rows)
        chart = alt.Chart(df).mark_line(point=True).encode(
            x="day:T", y="total_usd:Q", color="model:N",
        )
        st.altair_chart(chart, use_container_width=True)
        st.dataframe(df, use_container_width=True)

with tab_queue:
    st.header("Queue status")
    from tradingagents.dashboard.panels.queue import (
        fetch_queue_depth, fetch_recent_jobs, fetch_worker_heartbeat,
    )
    cols = st.columns(4)
    depth = fetch_queue_depth(_conn())
    for col, state in zip(cols, ["queued", "running", "done", "error"]):
        col.metric(state, depth.get(state, 0))
    st.subheader("Recent jobs")
    st.dataframe(fetch_recent_jobs(_conn(), limit=10), use_container_width=True)
    st.caption(f"worker heartbeat: {fetch_worker_heartbeat(_conn()) or '(never)'}")

with tab_actions:
    st.header("Brief actions")
    from tradingagents.dashboard.panels.actions import (
        fetch_pending_actions, fetch_recent_actioned,
    )
    st.subheader("Pending")
    pending = fetch_pending_actions(_conn())
    st.dataframe(pending or [{"info": "no pending"}], use_container_width=True)
    st.subheader("Recently actioned")
    actioned = fetch_recent_actioned(_conn(), limit=20)
    st.dataframe(actioned or [{"info": "none yet"}], use_container_width=True)


# Refinement-form route: ?brief_id=<id>
qp = st.query_params
if "brief_id" in qp:
    from tradingagents.dashboard.action_form import submit_backtest, submit_refinement
    bid = qp["brief_id"]
    st.divider()
    st.header(f"Follow up on brief {bid}")
    with st.form("action_form"):
        do_backtest = st.checkbox("Run backtest on these strategies")
        refinement = st.text_area("Refinement (free text)", "")
        submitted = st.form_submit_button("Submit")
    if submitted:
        if do_backtest:
            aid = submit_backtest(conn=_conn(), brief_id=bid, config=DEFAULT_CONFIG)
            st.success(f"Backtest queued (action_id={aid})")
        if refinement.strip():
            aid = submit_refinement(
                conn=_conn(), brief_id=bid, reply_text=refinement, config=DEFAULT_CONFIG,
            )
            st.success(f"Refinement queued (action_id={aid})")
