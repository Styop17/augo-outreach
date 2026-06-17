import streamlit as st
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    for _key in ["EXA_API_KEY", "ANTHROPIC_API_KEY", "ATTIO_API_KEY"]:
        if _key in st.secrets and not os.environ.get(_key):
            os.environ[_key] = st.secrets[_key]
except Exception:
    pass  # running locally with .env — keys already loaded by pipeline

from pipeline import find_coaches, fetch_page_content, research_coach, draft_email, draft_dm, country_from_query, _query_wants_clubs
from attio import get_existing_domains, push_coach

st.set_page_config(page_title="augo Outreach", page_icon="🏃", layout="wide")

if not st.session_state.get("authenticated"):
    st.title("augo Coach Outreach")
    password = st.text_input("Password", type="password")
    if st.button("Enter"):
        try:
            app_password = st.secrets.get("APP_PASSWORD", "")
        except Exception:
            app_password = os.environ.get("APP_PASSWORD", "")
        if password == app_password:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Wrong password")
    st.stop()

st.title("augo Coach Outreach")
st.caption("Find endurance coaches and clubs, draft personalised messages, push to Attio.")

query = st.text_input("Search query", placeholder="triathlon coaches in Amsterdam")
limit = st.slider("How many to find", min_value=1, max_value=20, value=5)

CHANNEL_BADGE = {
    "instagram": "🟢",
    "email":     "🟢",
    "phone":     "🟡",
    "linkedin":  "🟡",
    "website":   "🟡",
}


def run_pipeline(query, limit):
    results = []

    status = st.status("Finding coaches...", expanded=True)

    with status:
        country_code = country_from_query(query)
        st.write("Checking Attio for already-found coaches...")
        existing_domains = get_existing_domains(country_code=country_code)
        if existing_domains:
            st.write(f"Excluding {len(existing_domains)} already-found domains in this country.")

        allow_clubs = _query_wants_clubs(query)
        candidates = find_coaches(query, limit, extra_exclude=existing_domains)
        st.write(f"Found {len(candidates)} candidate pages — fetching until {limit} coaches found...")

        progress = st.progress(0)
        coaches_found = 0
        seen_root_domains = set()

        for candidate in candidates:
            if coaches_found >= limit:
                break

            from urllib.parse import urlparse
            root = urlparse(candidate.url).netloc.replace("www.", "").lower()
            if root in seen_root_domains:
                continue
            seen_root_domains.add(root)

            page = fetch_page_content(candidate)
            if not page:
                continue

            coaches_from_page = research_coach(page, allow_clubs=allow_clubs)
            if not coaches_from_page:
                continue

            for coach_data in coaches_from_page:
                coach_data["country_code"] = country_code
                if coaches_found >= limit:
                    break

                coaches_found += 1
                progress.progress(min(coaches_found / limit, 1.0))

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

                research_notes = draft.get("research_notes", "")
                ok, err = push_coach(coach_data, message, subject=subject, research_notes=research_notes)
                if not ok:
                    st.warning(f"Attio push failed for {coach_data['name']}: {err}")

                results.append({
                    "name":                 coach_data["name"],
                    "entity_type":          coach_data.get("entity_type", "coach"),
                    "athlete_count_signal": coach_data.get("athlete_count_signal", "unknown"),
                    "tools_mentioned":      ", ".join(coach_data.get("tools_mentioned", [])),
                    "channel":              channel,
                    "instagram_url":        coach_data.get("instagram_url", ""),
                    "phone":                coach_data.get("phone", ""),
                    "email":                coach_data.get("email", ""),
                    "linkedin_url":         coach_data.get("linkedin_url", ""),
                    "website":              coach_data.get("website", ""),
                    "research_notes":       research_notes,
                    "subject":              subject,
                    "message":              message,
                    "attio_pushed":         ok,
                })

        status.update(label=f"Done — {len(results)} coaches pushed to Attio", state="complete")

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
            if r["linkedin_url"]:
                st.markdown(f"[LinkedIn]({r['linkedin_url']})")
            if r["website"]:
                label = "Contact via website" if r["channel"] == "website" else "Website"
                st.markdown(f"[{label}]({r['website']})")
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
    pushed = sum(1 for r in st.session_state.results if r.get("attio_pushed"))
    st.divider()
    st.success(f"{pushed} / {len(st.session_state.results)} coaches pushed to Attio Sales Pipeline")
