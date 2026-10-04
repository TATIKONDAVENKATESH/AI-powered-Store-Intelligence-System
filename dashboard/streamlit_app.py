"""
Live Store Intelligence Dashboard — Part E (Bonus +10 points)

Real-time dashboard that polls all API endpoints every 5 seconds and shows:
- Live system health with camera feed status
- Per-store: unique visitors, conversion rate, queue depth, abandonment rate
- 4-stage conversion funnel with drop-off visualisation
- Zone heatmap with visit frequency and dwell times
- Active operational anomalies (BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE)

Proves the pipeline and API are genuinely connected — not just batch-processed.
"""

import os
import time

import pandas as pd
import requests
import streamlit as st

# ── Config ────────────────────────────────────────────────────────────────────
API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
STORE_IDS = os.getenv("STORE_IDS", "ST1076,ST1008").split(",")
REFRESH_S = int(os.getenv("REFRESH_S", "5"))

st.set_page_config(
    page_title="Store Intelligence — Live",
    page_icon="🏪",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS overrides ─────────────────────────────────────────────────────────────
st.markdown(
    """
<style>
    /* Dark header bar */
    [data-testid="stHeader"] { background: #0f1117; }
    /* Metric labels */
    [data-testid="stMetricLabel"] { font-size: 0.75rem; color: #888; }
    /* Anomaly pill */
    .anomaly-critical { background:#3d1a1a; border-left:4px solid #ff4b4b;
                         padding:8px 12px; border-radius:4px; margin:4px 0; }
    .anomaly-warn     { background:#3d2e00; border-left:4px solid #ffa500;
                         padding:8px 12px; border-radius:4px; margin:4px 0; }
    .anomaly-info     { background:#1a2a3d; border-left:4px solid #4b9fff;
                         padding:8px 12px; border-radius:4px; margin:4px 0; }
    /* Live indicator pulse */
    @keyframes pulse { 0%{opacity:1} 50%{opacity:.4} 100%{opacity:1} }
    .live-dot { display:inline-block; width:8px; height:8px; border-radius:50%;
                background:#00cc66; animation:pulse 1.5s infinite; margin-right:6px; }
</style>
""",
    unsafe_allow_html=True,
)


# ── API helpers ───────────────────────────────────────────────────────────────
def fetch(endpoint: str) -> dict | None:
    try:
        r = requests.get(f"{API_URL}{endpoint}", timeout=4)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def severity_class(sev: str) -> str:
    return {"CRITICAL": "anomaly-critical", "WARN": "anomaly-warn"}.get(
        sev, "anomaly-info"
    )


def severity_icon(sev: str) -> str:
    return {"CRITICAL": "🔴", "WARN": "🟡", "INFO": "🔵"}.get(sev, "⚪")


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## ⚙️ Controls")
    refresh = st.slider("Refresh interval (s)", 2, 30, REFRESH_S)
    stores_selected = st.multiselect("Stores to display", STORE_IDS, default=STORE_IDS)
    st.divider()
    st.markdown(f"**API:** `{API_URL}`")
    st.markdown(f"**Stores:** {', '.join(STORE_IDS)}")
    st.markdown("---")
    st.markdown("### Legend")
    st.markdown("🟢 System OK  \n🟡 Degraded  \n🔴 Down")


# ── Main loop ─────────────────────────────────────────────────────────────────
placeholder = st.empty()

while True:
    ts = time.strftime("%H:%M:%S")

    with placeholder.container():
        # ── Header row ──────────────────────────────────────────────────────
        col_title, col_live, col_ts = st.columns([4, 1, 1])
        col_title.markdown("# 🏪 Store Intelligence — Live")
        col_live.markdown(
            '<span class="live-dot"></span><small>LIVE</small>',
            unsafe_allow_html=True,
        )
        col_ts.markdown(f"<small>Updated: **{ts}**</small>", unsafe_allow_html=True)

        # ── Health banner ───────────────────────────────────────────────────
        health = fetch("/health")
        if health:
            status = health.get("status", "unknown")
            db_ok = health.get("db_connected", False)
            stale = health.get("stale_feed", False)
            icon = "🟢" if status == "ok" else ("🟡" if status == "degraded" else "🔴")

            h1, h2, h3, h4 = st.columns(4)
            h1.metric("System Status", f"{icon} {status.upper()}")
            h2.metric("DB Connected", "✅ Yes" if db_ok else "❌ No")
            h3.metric("Stale Feeds", "⚠️ Yes" if stale else "✅ None")
            feeds = health.get("store_feeds", [])
            h4.metric("Active Cameras", str(len(feeds)))

            # Camera feed table
            if feeds:
                with st.expander("📷 Camera feed status", expanded=False):
                    df_feeds = pd.DataFrame(feeds)
                    df_feeds["stale"] = df_feeds["stale"].map(
                        {True: "⚠️ Stale", False: "✅ Live"}
                    )
                    st.dataframe(df_feeds, use_container_width=True, hide_index=True)
        else:
            st.error(
                f"⛔ **API unreachable at {API_URL}** — "
                "is `docker compose up` running?  "
                "Or set `API_URL` env var to your API address."
            )

        st.divider()

        # ── Per-store panels ─────────────────────────────────────────────────
        for store_id in stores_selected or STORE_IDS:
            st.markdown(f"## 🏪 `{store_id}`")
            tabs = st.tabs(["📊 Metrics", "🔽 Funnel", "🗺️ Heatmap", "⚠️ Anomalies"])

            # ── Metrics tab ─────────────────────────────────────────────────
            with tabs[0]:
                m = fetch(f"/stores/{store_id}/metrics")
                if m:
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.metric("Unique Visitors", m.get("unique_visitors", 0))
                    c2.metric("Conversion Rate", f"{m.get('conversion_rate', 0):.1%}")
                    c3.metric("Queue Depth", m.get("queue_depth", 0))
                    c4.metric("Abandonment Rate", f"{m.get('abandonment_rate', 0):.1%}")
                    c5.metric("POS Transactions", m.get("total_transactions", 0))

                    dwells = m.get("avg_dwell_per_zone", [])
                    if dwells:
                        st.markdown("**Average dwell time per zone (seconds)**")
                        df_dwell = pd.DataFrame(dwells)
                        st.bar_chart(df_dwell.set_index("zone_id")["avg_dwell_seconds"])
                else:
                    st.warning(f"Metrics unavailable for {store_id}")

            # ── Funnel tab ──────────────────────────────────────────────────
            with tabs[1]:
                funnel = fetch(f"/stores/{store_id}/funnel")
                if funnel and funnel.get("stages"):
                    stages = funnel["stages"]
                    fcols = st.columns(len(stages))
                    for i, stage in enumerate(stages):
                        delta = (
                            f"−{stage['drop_off_pct']:.1f}% drop-off"
                            if stage["drop_off_pct"] > 0
                            else None
                        )
                        fcols[i].metric(
                            stage["stage"],
                            f"{stage['count']:,}",
                            delta=delta,
                            delta_color="inverse",
                        )

                    # Funnel bar chart
                    df_funnel = pd.DataFrame(stages)
                    st.markdown("**Funnel counts**")
                    st.bar_chart(df_funnel.set_index("stage")["count"])
                else:
                    st.info("No funnel data yet — ingest some events first.")

            # ── Heatmap tab ─────────────────────────────────────────────────
            with tabs[2]:
                heatmap = fetch(f"/stores/{store_id}/heatmap")
                if heatmap and heatmap.get("zones"):
                    zones = heatmap["zones"]
                    df_heat = pd.DataFrame(zones)

                    # Show normalised score as horizontal bar chart
                    st.markdown("**Zone engagement score (normalised 0–100)**")
                    st.bar_chart(df_heat.set_index("zone_id")["normalised_score"])

                    # Detailed table
                    df_display = df_heat[
                        [
                            "zone_id",
                            "visit_frequency",
                            "avg_dwell_seconds",
                            "normalised_score",
                            "data_confidence",
                        ]
                    ].copy()
                    df_display["data_confidence"] = df_display["data_confidence"].map(
                        {True: "✅ High", False: "⚠️ Low (<20 sessions)"}
                    )
                    df_display.columns = [
                        "Zone",
                        "Visits",
                        "Avg Dwell (s)",
                        "Score",
                        "Confidence",
                    ]
                    st.dataframe(df_display, use_container_width=True, hide_index=True)
                else:
                    st.info("No heatmap data yet.")

            # ── Anomalies tab ────────────────────────────────────────────────
            with tabs[3]:
                anom = fetch(f"/stores/{store_id}/anomalies")
                if anom:
                    anomalies = anom.get("anomalies", [])
                    if anomalies:
                        for a in anomalies:
                            css = severity_class(a["severity"])
                            icon = severity_icon(a["severity"])
                            st.markdown(
                                f'<div class="{css}">'
                                f"{icon} <strong>{a['anomaly_type']}</strong> "
                                f"<em>({a['severity']})</em><br>"
                                f"{a['description']}<br>"
                                f"<small>Action: {a['suggested_action']}</small>"
                                f"</div>",
                                unsafe_allow_html=True,
                            )
                    else:
                        st.success("✅ No active anomalies detected.")
                else:
                    st.warning("Anomaly data unavailable.")

        # ── Footer ───────────────────────────────────────────────────────────
        st.divider()
        st.caption(
            f"🔄 Auto-refreshing every **{refresh}s** · "
            f"API: `{API_URL}` · "
            f"[API Docs]({API_URL}/docs)"
        )

    time.sleep(refresh)
