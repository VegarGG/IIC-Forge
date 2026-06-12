"""Streamlit dashboard entrypoint.

Run via: streamlit run tradingagents/dashboard/app.py
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.db import connect as iic_connect
from tradingagents.dashboard.panels.briefs import fetch_recent_briefs, fetch_brief_thread


st.set_page_config(page_title="IIC-FORGE Dashboard", layout="wide")


@st.cache_resource
def _conn():
    return iic_connect(DEFAULT_CONFIG["iic_db_path"])


st.title("IIC-FORGE")

tab_ops, tab_briefs, tab_costs, tab_queue, tab_actions = st.tabs(
    ["Operations", "Briefs", "Costs", "Queue", "Actions"]
)

with tab_ops:
    st.header("Operational status")
    from datetime import datetime, timezone
    from tradingagents.dashboard.panels.operations import fetch_operations_snapshot

    snap = fetch_operations_snapshot(_conn(), now_ts=datetime.now(timezone.utc).isoformat())
    source_rows = list(snap["sources"].values())
    st.subheader("Sources")
    st.dataframe(source_rows or [{"info": "no source health rows yet"}], use_container_width=True)
    st.subheader("LLM calls")
    llm_rows = [{"role": role, **values} for role, values in snap["llm_calls"].items()]
    st.dataframe(llm_rows or [{"info": "no llm call rows yet"}], use_container_width=True)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Deferred pending", snap["deferred_salience"].get("pending", 0))
    c2.metric("Deferred dead", snap["deferred_salience"].get("dead", 0))
    c3.metric("Deferred orphaned", snap["deferred_salience"].get("orphaned_events", 0))
    c4.metric("Failed delivery groups", len(snap["delivery_groups"]["failed"]))
    c5.metric("Skipped-only groups", snap["delivery_groups"]["skipped_only"])
    st.subheader("Queue lanes")
    lane_rows = [
        {"lane": lane, **states}
        for lane, states in snap["queue_lanes"].items()
    ]
    st.dataframe(lane_rows or [{"info": "no queue jobs yet"}], use_container_width=True)

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
                body_path = Path(DEFAULT_CONFIG["iic_data_dir"]) / b["content_path"]
                if body_path.exists():
                    st.markdown(body_path.read_text())

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
