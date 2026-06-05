"""Live store intelligence dashboard. Polls all API endpoints every 5 seconds."""
import time
import os
import requests
import streamlit as st

API_URL     = os.getenv("API_URL", "https://ai-powered-store-intelligence-system.onrender.com/")
STORE_IDS   = ["ST1076", "ST1008"]
REFRESH_S   = 5

st.set_page_config(page_title="Store Intelligence", layout="wide")
st.title("📊 Store Intelligence — Live Dashboard")


def fetch(endpoint: str) -> dict | None:
    try:
        r = requests.get(f"{API_URL}{endpoint}", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return None


placeholder = st.empty()

while True:
    with placeholder.container():
        # Health row
        health = fetch("/health")
        if health:
            db_ok = health.get("db_connected", False)
            stale = health.get("stale_feed", False)
            status = health.get("status", "unknown")
            color  = "🟢" if status == "ok" else ("🟡" if status == "degraded" else "🔴")
            st.markdown(f"**System Status:** {color} `{status}` | DB: {'✅' if db_ok else '❌'} | Stale feeds: {'⚠️' if stale else '✅'}")
        else:
            st.error("API unreachable — is `docker compose up` running?")

        for store_id in STORE_IDS:
            st.markdown(f"---\n## 🏪 Store: `{store_id}`")
            col1, col2, col3, col4 = st.columns(4)

            # Metrics
            m = fetch(f"/stores/{store_id}/metrics")
            if m:
                col1.metric("Unique Visitors",    m.get("unique_visitors", 0))
                col2.metric("Conversion Rate",    f"{m.get('conversion_rate', 0):.1%}")
                col3.metric("Queue Depth",        m.get("queue_depth", 0))
                col4.metric("Abandonment Rate",   f"{m.get('abandonment_rate', 0):.1%}")
            else:
                col1.warning("Metrics unavailable")

            # Funnel
            funnel = fetch(f"/stores/{store_id}/funnel")
            if funnel and funnel.get("stages"):
                st.markdown("**Conversion Funnel**")
                fcols = st.columns(len(funnel["stages"]))
                for i, stage in enumerate(funnel["stages"]):
                    fcols[i].metric(
                        stage["stage"],
                        stage["count"],
                        delta=f"-{stage['drop_off_pct']:.1f}%" if stage["drop_off_pct"] > 0 else None,
                        delta_color="inverse",
                    )

            # Heatmap
            heatmap = fetch(f"/stores/{store_id}/heatmap")
            if heatmap and heatmap.get("zones"):
                st.markdown("**Zone Heatmap** (visit frequency, normalised 0–100)")
                hcols = st.columns(min(len(heatmap["zones"]), 4))
                for i, zone in enumerate(heatmap["zones"][:4]):
                    hcols[i % 4].metric(
                        zone["zone_id"],
                        f"{zone['normalised_score']:.0f}",
                        help=f"Visits: {zone['visit_frequency']} | Avg dwell: {zone['avg_dwell_seconds']:.0f}s | Confidence: {'✅' if zone['data_confidence'] else '⚠️'}",
                    )

            # Anomalies
            anom = fetch(f"/stores/{store_id}/anomalies")
            if anom and anom.get("anomalies"):
                st.markdown("**⚠️ Active Anomalies**")
                for a in anom["anomalies"]:
                    sev    = a["severity"]
                    colour = "🔴" if sev == "CRITICAL" else ("🟡" if sev == "WARN" else "🔵")
                    st.warning(f"{colour} **{a['anomaly_type']}** ({sev}): {a['description']}  \n_Action: {a['suggested_action']}_")

        st.caption(f"Last refreshed: {time.strftime('%H:%M:%S')}")

    time.sleep(REFRESH_S)