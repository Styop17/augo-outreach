import csv
import requests
from pipeline import extract_coach_info

CSV_FILE = "triathlon_coaches_madrid_spain.csv"

with open(CSV_FILE, newline="", encoding="utf-8") as f:
    coaches = list(csv.DictReader(f))

headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

for coach in coaches:
    name    = coach.get("name", "")
    website = coach.get("website", "")

    print(f"\n{name}")
    print(f"  Website: {website}")

    if not website:
        print("  Channel: no_contact_found (no website)")
        continue

    try:
        resp = requests.get(website, headers=headers, timeout=10)
        page_text = resp.text
    except Exception as e:
        print(f"  Could not fetch website: {e}")
        continue

    info = extract_coach_info(page_text, website)

    email     = info.get("email", "")
    phone     = info.get("phone", "")
    contact   = info.get("has_contact_form", False)
    instagram = info.get("instagram_url", "")
    linkedin  = info.get("linkedin_url", "")

    if email:
        channel = f"email → {email}"
    elif contact:
        channel = f"contact_form → {website}"
    elif phone:
        channel = f"phone → {phone}"
    elif instagram:
        channel = f"instagram → {instagram}"
    elif linkedin:
        channel = f"linkedin → {linkedin}"
    else:
        channel = "no_contact_found"

    print(f"  Channel: {channel}")
