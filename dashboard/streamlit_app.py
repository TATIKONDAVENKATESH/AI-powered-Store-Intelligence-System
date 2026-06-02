"""
streamlit_app.py — Store Intelligence Live Dashboard
Run: streamlit run dashboard/streamlit_app.py
"""
from __future__ import annotations
import time
import os
import requests
import streamlit as st
import pandas as pd

API_BASE  = os.getenv("API_URL", "http://localhost:8000")
STORE_ID  = os.getenv("STORE_ID", "STORE_BLR_002")
REFRESH_S = 5   # auto-refresh interval in seconds

st.set_page_config(
    page_title="Store Intelligence",
    page_icon="🛍️",
    layout="wide",
)


def fetch(path: str) -> dict | None:
    """GET from API, return JSON or None on error."""
    try:
        r = requests.get(f"{API_BASE}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"API error ({path}): {exc}")
        return None


# --- Header ---
st.title("🛍️ Store Intelligence Dashboard")
st.caption(f"Store: **{STORE_ID}** · API: {API_BASE} · Auto-refresh every {REFRESH_S}s")

# --- Health badge ---
health = fetch("/health")
if health:
    colour = {"ok": "🟢", "degraded": "🟡", "down": "🔴"}.get(health["status"], "⚪")
    st.markdown(f"{colour} System status: **{health['status'].upper()}** &nbsp;|&nbsp; "
                f"DB connected: `{health['db_connected']}` &nbsp;|&nbsp; "
                f"Last event: `{health.get('last_event_at', 'N/A')}`")

st.divider()

# --- Row 1: Key Metrics ---
metrics = fetch(f"/stores/{STORE_ID}/metrics")
if metrics:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Unique Visitors",    metrics["unique_visitors"])
    c2.metric("Conversion Rate",    f"{metrics['conversion_rate']:.1%}")
    c3.metric("Transactions",       metrics["total_transactions"])
    c4.metric("Queue Depth",        metrics["queue_depth"])
    c5.metric("Abandonment Rate",   f"{metrics['abandonment_rate']:.1%}")

st.divider()

# --- Row 2: Funnel + Heatmap ---
col_funnel, col_heat = st.columns(2)

with col_funnel:
    st.subheader("Conversion Funnel")
    funnel = fetch(f"/stores/{STORE_ID}/funnel")
    if funnel and funnel.get("stages"):
        df = pd.DataFrame(funnel["stages"])
        st.bar_chart(df.set_index("stage")["count"])
        st.dataframe(
            df[["stage", "count", "drop_off_pct"]].rename(
                columns={"drop_off_pct": "drop_off %"}
            ),
            use_container_width=True,
            hide_index=True,
        )

with col_heat:
    st.subheader("Zone Heatmap (Dwell & Visits)")
    heatmap = fetch(f"/stores/{STORE_ID}/heatmap")
    if heatmap and heatmap.get("zones"):
        df = pd.DataFrame(heatmap["zones"])
        st.dataframe(
            df[["zone_id", "visit_frequency", "avg_dwell_seconds", "normalised_score", "data_confidence"]]
            .sort_values("normalised_score", ascending=False)
            .rename(columns={
                "zone_id": "Zone",
                "visit_frequency": "Visits",
                "avg_dwell_seconds": "Avg Dwell (s)",
                "normalised_score": "Score (0-100)",
                "data_confidence": "Confident",
            }),
            use_container_width=True,
            hide_index=True,
        )
    elif heatmap:
        st.info("No zone data yet.")

st.divider()

# --- Row 3: Anomalies ---
st.subheader("⚠️ Active Anomalies")
anomalies = fetch(f"/stores/{STORE_ID}/anomalies")
if anomalies:
    items = anomalies.get("anomalies", [])
    if not items:
        st.success("No active anomalies.")
    else:
        for a in items:
            colour_map = {"CRITICAL": "🔴", "WARN": "🟡", "INFO": "🔵"}
            icon = colour_map.get(a["severity"], "⚪")
            with st.expander(f"{icon} {a['anomaly_type']} — {a['severity']}"):
                st.write(f"**Description:** {a['description']}")
                st.write(f"**Suggested action:** {a['suggested_action']}")
                st.caption(f"Detected at: {a['detected_at']}")

st.divider()

# --- Row 4: Camera Feed Status ---
if health and health.get("store_feeds"):
    st.subheader("📷 Camera Feed Status")
    df = pd.DataFrame(health["store_feeds"])
    df["status"] = df["stale"].map({True: "🟡 Stale", False: "🟢 Live"})
    st.dataframe(
        df[["camera_id", "status", "last_event_at"]].rename(
            columns={"camera_id": "Camera", "last_event_at": "Last Event"}
        ),
        use_container_width=True,
        hide_index=True,
    )

# --- Auto-refresh ---
st.caption(f"_Last refreshed: {time.strftime('%H:%M:%S')}_")
if st.button("🔄 Refresh Dashboard"):
    st.rerun()