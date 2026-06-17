"""
One-time script to redraft messages for coaches already in Attio.
Fetches each person's website content, redrafts with the latest prompt,
and patches draft_message + draft_message_5 on their list entry.

Usage:
    .venv/bin/python redraft.py
"""

import os
import sys
import json
import requests
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pipeline import draft_dm, draft_email, _clean

ATTIO_API_KEY = os.getenv("ATTIO_API_KEY")
BASE          = "https://api.attio.com/v2"
LIST_SLUG     = "sales_pipeline"
PEOPLE_OBJECT = "people"

REQ_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _h():
    return {"Authorization": f"Bearer {ATTIO_API_KEY}", "Content-Type": "application/json"}


def fetch_website_text(url: str) -> str:
    if not url:
        return ""
    try:
        resp = requests.get(f"https://{url}", headers=REQ_HEADERS, timeout=10)
        if not resp.ok:
            resp = requests.get(f"http://{url}", headers=REQ_HEADERS, timeout=10)
        # Strip tags crudely — good enough for Claude
        import re
        text = re.sub(r'<[^>]+>', ' ', resp.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:4000]
    except Exception:
        return ""


def get_all_entries():
    entries = []
    offset  = 0
    while True:
        resp = requests.post(
            f"{BASE}/lists/{LIST_SLUG}/entries/query",
            headers=_h(),
            json={"limit": 500, "offset": offset},
        )
        if not resp.ok:
            break
        data = resp.json().get("data", [])
        entries.extend(data)
        if len(data) < 500:
            break
        offset += 500
    return entries


def get_person(record_id: str) -> dict:
    resp = requests.get(
        f"{BASE}/objects/{PEOPLE_OBJECT}/records/{record_id}", headers=_h()
    )
    if not resp.ok:
        return {}
    return resp.json().get("data", {}).get("values", {})


def get_company_domain(company_id: str) -> str:
    resp = requests.get(
        f"{BASE}/objects/companies/records/{company_id}", headers=_h()
    )
    if not resp.ok:
        return ""
    domains = resp.json().get("data", {}).get("values", {}).get("domains", [])
    return domains[0].get("domain", "") if domains else ""


def patch_entry(entry_id: str, message: str):
    resp = requests.patch(
        f"{BASE}/lists/{LIST_SLUG}/entries/{entry_id}",
        headers=_h(),
        json={"data": {"entry_values": {
            "draft_message":   [{"value": message}],
            "draft_message_5": [{"value": message}],
        }}},
    )
    return resp.ok, resp.status_code


def parse_description(desc_text: str) -> dict:
    """Parse the structured description we wrote into Attio back into fields."""
    result = {}
    for line in desc_text.splitlines():
        if line.startswith("Type:"):
            result["entity_type"] = line.split(":", 1)[1].strip()
        elif line.startswith("Athletes:"):
            result["athlete_count_signal"] = line.split(":", 1)[1].strip()
        elif line.startswith("Tools:"):
            tools_str = line.split(":", 1)[1].strip()
            result["tools_mentioned"] = [t.strip() for t in tools_str.split(",") if t.strip()]
    return result


def main():
    print("Fetching Sales Pipeline entries from Attio...\n")
    entries = get_all_entries()
    print(f"Found {len(entries)} total entries.\n")

    coaches = []
    for entry in entries:
        entry_id  = entry.get("id", {}).get("entry_id")
        record_id = entry.get("parent_record_id")
        if not entry_id or not record_id:
            continue

        # Only process entries that have a draft_message already set (added by this pipeline)
        entry_vals = entry.get("entry_values", {})
        existing_msg = ""
        for m in entry_vals.get("draft_message", []):
            existing_msg = m.get("value", "")
            break
        if not existing_msg:
            continue

        person_vals = get_person(record_id)
        if not person_vals:
            continue

        # Name
        name = ""
        for n in person_vals.get("name", []):
            name = n.get("full_name", "") or f"{n.get('first_name','')} {n.get('last_name','')}".strip()
            break
        if not name:
            continue

        # Channel signals
        instagram_url = ""
        for ig in person_vals.get("instagram", []):
            instagram_url = ig.get("value", "")
            break

        email = ""
        for em in person_vals.get("email_addresses", []):
            email = em.get("email_address", "")
            break

        linkedin_url = ""
        for li in person_vals.get("linkedin", []):
            linkedin_url = li.get("value", "")
            break

        # Website via company
        website_domain = ""
        for co in person_vals.get("company", []):
            company_id = co.get("target_record_id")
            if company_id:
                website_domain = get_company_domain(company_id)
                break

        # Description fields
        desc_text = ""
        for d in person_vals.get("description", []):
            desc_text = d.get("value", "")
            break
        meta = parse_description(desc_text)

        # Determine channel
        if instagram_url:
            channel = "instagram"
        elif email:
            channel = "email"
        elif linkedin_url:
            channel = "linkedin"
        elif website_domain:
            channel = "website"
        else:
            channel = "website"

        coaches.append({
            "entry_id":             entry_id,
            "name":                 name,
            "channel":              channel,
            "instagram_url":        instagram_url,
            "email":                email,
            "linkedin_url":         linkedin_url,
            "website":              f"https://{website_domain}" if website_domain else "",
            "website_domain":       website_domain,
            "entity_type":          meta.get("entity_type", "coach"),
            "athlete_count_signal": meta.get("athlete_count_signal", "unknown"),
            "tools_mentioned":      meta.get("tools_mentioned", []),
            "website_text":         "",
            "existing_message":     existing_msg,
        })

    print(f"Entries with an existing draft message: {len(coaches)}\n")
    for c in coaches:
        print(f"  - {c['name']} | channel: {c['channel']} | website: {c['website_domain']}")

    if not coaches:
        print("\nNothing to update.")
        return

    confirm = input("\nRedraft all of the above? (y/n): ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        return

    print()
    for c in coaches:
        print(f"[{c['name']}] Fetching website content...", end=" ", flush=True)
        c["website_text"] = fetch_website_text(c["website_domain"])
        print("done")

        print(f"[{c['name']}] Drafting new message...", end=" ", flush=True)
        try:
            if c["channel"] == "email":
                draft = draft_email(c)
                message = draft.get("message", "")
            else:
                draft = draft_dm(c)
                message = draft.get("dm_message", "")
        except Exception as e:
            print(f"FAILED — {e}")
            continue
        print("done")

        print(f"[{c['name']}] New message:\n  {message}\n")

        ok, status = patch_entry(c["entry_id"], message)
        if ok:
            print(f"[{c['name']}] Attio updated.\n")
        else:
            print(f"[{c['name']}] Attio patch FAILED (status {status}).\n")


if __name__ == "__main__":
    main()
