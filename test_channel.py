import json
from pipeline import extract_coach_info, channel_from_url

with open("coaches.json") as f:
    coaches = json.load(f)

for coach in coaches:
    name         = coach.get("name", "")
    website      = coach.get("website", "")
    website_text = coach.get("website_text", "")
    hook_results = coach.get("hook_results", [])

    print(f"\n{name}")
    print(f"  Found at: {website}")

    # if original page is social media, find real website from hook results
    real_website = website
    if channel_from_url(website) and hook_results:
        for url in hook_results[0]["urls"]:
            if not channel_from_url(url):
                real_website = url
                print(f"  Real website found: {real_website}")
                break

    info = extract_coach_info(website_text, real_website)

    email     = info.get("email", "")
    contact   = info.get("has_contact_form", False)
    instagram = info.get("instagram_url", "")
    linkedin  = info.get("linkedin_url", "")
    url_ch    = channel_from_url(website)

    if email:
        channel = f"email → {email}"
    elif contact:
        channel = f"contact_form → {real_website}"
    elif url_ch:
        channel = f"{url_ch} → {website}"
    elif instagram:
        channel = f"instagram → {instagram}"
    elif linkedin:
        channel = f"linkedin → {linkedin}"
    else:
        channel = "no_contact_found"

    print(f"  Channel: {channel}")
