import streamlit as st
import csv
import io
import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Streamlit Cloud stores secrets in st.secrets; inject them as env vars
# so pipeline.py's os.getenv() calls work in both local and cloud environments
for _key in ["EXA_API_KEY", "ANTHROPIC_API_KEY"]:
    if _key in st.secrets and not os.environ.get(_key):
        os.environ[_key] = st.secrets[_key]

from pipeline import (
    find_coaches, research_coach, draft_email, draft_dm,
    load_seen, save_seen, seen_key,
)

st.set_page_config(page_title="augo Outreach", page_icon="🏃", layout="wide")

if not st.session_state.get("authenticated"):
    st.title("augo Coach Outreach")
    password = st.text_input("Password", type="password")
    if st.button("Enter"):
        if password == st.secrets.get("APP_PASSWORD", ""):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()

st.title("augo Coach Outreach")
st.caption("Find endurance coaches, draft personalised messages, export to CSV.")

query = st.text_input("Search query", placeholder="triathlon coaches in Amsterdam")
limit = st.slider("Coaches to find", min_value=1, max_value=20, value=5)

CHANNEL_BADGE = {
    "instagram": "🟢",
    "email":     "🟢",
    "phone":     "🟡",
    "facebook":  "🟡",
}


def run_pipeline(query, limit):
    seen    = load_seen()
    results = []

    status = st.status("Finding coaches...", expanded=True)

    with status:
        raw_coaches = find_coaches(query, limit)
        st.write(f"Found {len(raw_coaches)} pages — researching each one...")

        progress = st.progress(0)

        for i, page in enumerate(raw_coaches):
            coach_data = research_coach(page, seen)
            progress.progress((i + 1) / len(raw_coaches))

            if not coach_data:
                continue

            channel = coach_data.get("channel", "")

            try:
                if channel == "email":
                    draft   = draft_email(coach_data)
                    subject = draft.get("subject", "")
                    message = draft.get("message", "")
                else:
                    draft   = draft_dm(coach_data)
                    subject = ""
                    message = draft.get("dm_message", "")
            except Exception as e:
                st.warning(f"Could not draft message for {coach_data['name']}: {e}")
                continue

            seen.add(seen_key(coach_data["name"], coach_data.get("email", "")))
            save_seen(seen)

            results.append({
                "name":           coach_data["name"],
                "channel":        channel,
                "instagram_url":  coach_data.get("instagram_url", ""),
                "phone":          coach_data.get("phone", ""),
                "email":          coach_data.get("email", ""),
                "facebook_url":   coach_data.get("facebook_url", ""),
                "website":        coach_data.get("website", ""),
                "research_notes": draft.get("research_notes", ""),
                "subject":        subject,
                "message":        message,
            })

        status.update(label=f"Done — {len(results)} coaches processed", state="complete")

    return results


if "results" not in st.session_state:
    st.session_state.results = []

if st.button("Run", type="primary", disabled=not query):
    st.session_state.results = run_pipeline(query, limit)

for i, r in enumerate(st.session_state.results):
    badge = CHANNEL_BADGE.get(r["channel"], "🔴")

    with st.expander(f"{badge} {r['name']} — {r['channel']}", expanded=True):
        col1, col2 = st.columns([1, 2])

        with col1:
            st.markdown("**Contact**")
            if r["instagram_url"]:
                handle = r["instagram_url"].rstrip("/").split("/")[-1]
                st.markdown(f"[Instagram]({r['instagram_url']}) — @{handle}")
            if r["email"]:
                st.markdown(f"✉️ {r['email']}")
            if r["phone"]:
                st.markdown(f"📞 {r['phone']}")
            if r["facebook_url"]:
                st.markdown(f"[Facebook]({r['facebook_url']})")
            if r["website"]:
                st.markdown(f"[Website]({r['website']})")
            if r["research_notes"]:
                st.divider()
                st.caption(r["research_notes"])

        with col2:
            if r["subject"]:
                st.markdown(f"**Subject:** {r['subject']}")
            st.text_area(
                "Message",
                value=r["message"],
                height=160,
                key=f"msg_{i}",
                label_visibility="collapsed",
            )

if st.session_state.results:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=st.session_state.results[0].keys())
    writer.writeheader()
    writer.writerows(st.session_state.results)

    slug = re.sub(r"[\s]+", "_", re.sub(r"[^\w\s-]", "", query.lower()).strip())

    st.divider()
    st.download_button(
        "⬇️ Download CSV",
        data=output.getvalue(),
        file_name=f"{slug}.csv",
        mime="text/csv",
        type="primary",
    )
