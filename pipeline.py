"""
augo Coach Outreach Pipeline
Finds endurance coaches via Exa, drafts personalised outreach messages using Claude,
and saves everything to a CSV.

Usage:
    python pipeline.py              — full run (Exa + Claude)
    python pipeline.py --fetch      — Exa only, saves coaches.json
    python pipeline.py --from-file  — Claude only, loads from coaches.json
    python pipeline.py --test       — fake data, no API calls except Claude
"""

import os
import re
import csv
import json
import sys
import requests
from dotenv import load_dotenv
import anthropic

def country_from_query(query: str) -> str:
    """Return ISO 3166-1 alpha-2 country code inferred from the search query."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=8,
        messages=[{"role": "user", "content":
            f"What country is implied by this search query? "
            f"Reply with only the ISO 3166-1 alpha-2 country code (e.g. CH, NL, DE). "
            f"If no country can be inferred, reply with an empty string.\n\nQuery: {query}"
        }]
    )
    code = resp.content[0].text.strip().upper()
    return code if len(code) == 2 and code.isalpha() else ""

load_dotenv()

# ── Colors ─────────────────────────────────────────────────────────────────────

GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

CHANNEL_COLOR = {
    "instagram":        GREEN,
    "email":            GREEN,
    "phone":            YELLOW,
    "linkedin":         YELLOW,
    "no_contact_found": RED,
}

# ── Config ─────────────────────────────────────────────────────────────────────

EXA_API_KEY       = os.getenv("EXA_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

SENDER_NAME = "Bruna"
SENDER_ROLE = "co-founder of augo"

# One-liner used in DMs/emails where space is tight
AUGO_PITCH_SHORT = (
    "augo is a tool that tells endurance coaches which of their athletes needs "
    "attention right now — so they stop losing time to admin and start coaching "
    "the way they used to when they had five athletes."
)

# Fuller version used in email body where there's more room
AUGO_PITCH_FULL = (
    "augo is the intelligence layer for human endurance coaching. "
    "It brings together athlete messages, workout data, and session feedback in "
    "one place and surfaces who needs attention — replacing the "
    "WhatsApp-TrainingPeaks-memory stack coaches currently run on. "
    "It sits next to TrainingPeaks, not instead of it. augo does what TP never will: "
    "tell you which of your athletes needs you right now."
)

AUGO_PITCH = AUGO_PITCH_SHORT  # kept for backward compat with test output

SKIP_HANDLES = {"p", "explore", "accounts", "stories", "reel", "reels", "tv",
                "sharer", "share", "pages", "groups"}


# ── Phase 1: Discovery ─────────────────────────────────────────────────────────

def _exa():
    from exa_py import Exa
    return Exa(EXA_API_KEY)


def find_coaches(query: str, target: int, extra_exclude: list = None):
    """URL-only search returning up to target*3 candidates (no content fetched yet)."""
    exclude = [
        "instagram.com", "strava.com", "linkedin.com",
        "facebook.com", "twitter.com", "youtube.com",
        "trainingpeaks.com", "tiktok.com",
    ]
    if extra_exclude:
        exclude += extra_exclude

    print(f"\nSearching for coaches...")
    print(f"  Query: {query}\n")

    results = _exa().search(
        query,
        num_results=min(target * 3, 50),
        exclude_domains=exclude,
    )

    print(f"  Found {len(results.results)} candidate pages\n")
    return results.results


def fetch_page_content(page):
    """Fetch text content for a single Exa result."""
    fetched = _exa().get_contents([page.id], text=True)
    return fetched.results[0] if fetched.results else None


# ── Phase 2: Contact Finding ───────────────────────────────────────────────────

def find_contact_info(page_text: str, website_url: str) -> dict:
    req_headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    def first_match(pattern, text):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1) if m else None

    # Fetch raw HTML to catch social icon links missed by Exa's text extraction
    raw_html = ""
    if website_url:
        try:
            raw_html = requests.get(website_url, headers=req_headers, timeout=8).text
        except Exception:
            pass

    search_text = page_text + "\n" + raw_html

    # Instagram — scan page first, then guess from domain
    instagram_url = ""
    handle = first_match(r'instagram\.com/([\w.]+)/?', search_text)
    if handle and handle.lower() not in SKIP_HANDLES:
        instagram_url = f"https://instagram.com/{handle}"

    if not instagram_url and website_url:
        from urllib.parse import urlparse
        domain = urlparse(website_url).netloc.lower().replace("www.", "")
        guess  = domain.split(".")[0]
        if guess and len(guess) > 2:
            try:
                resp = requests.get(f"https://www.instagram.com/{guess}/",
                                    headers=req_headers, timeout=6)
                if resp.status_code == 200 and "Page Not Found" not in resp.text:
                    instagram_url = f"https://instagram.com/{guess}"
            except Exception:
                pass

    # LinkedIn
    linkedin_url = ""
    h = first_match(r'linkedin\.com/in/([\w\-]+)/?', search_text)
    if h and h.lower() not in SKIP_HANDLES:
        linkedin_url = f"https://linkedin.com/in/{h}"

    # Phone — international format only
    phone = ""
    m = re.search(r'\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{3,5}[\s\-]?\d{3,5}', search_text)
    if m:
        phone = m.group(0).strip()

    # Email — skip image filenames and generic addresses
    email = ""
    image_exts = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp"}
    skip_locals = {"noreply", "support", "info", "hello", "contact", "admin", "mail"}
    for m in re.finditer(r'[\w.\-+]+@[\w.\-]+\.[a-z]{2,}', search_text, re.IGNORECASE):
        candidate = m.group(0)
        if candidate.rsplit(".", 1)[-1].lower() in image_exts:
            continue
        if candidate.split("@")[0].lower() in skip_locals:
            continue
        email = candidate
        break

    # Channel priority: instagram > phone > email > linkedin > website
    if instagram_url:
        channel = "instagram"
    elif phone:
        channel = "phone"
    elif email:
        channel = "email"
    elif linkedin_url:
        channel = "linkedin"
    elif website_url:
        channel = "website"
    else:
        channel = "no_contact_found"

    return {
        "channel":       channel,
        "instagram_url": instagram_url,
        "linkedin_url":  linkedin_url,
        "phone":         phone,
        "email":         email,
    }


# ── Phase 3: Research ──────────────────────────────────────────────────────────

def extract_coach_info(page_text: str, page_url: str) -> dict:
    client      = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    text_sample = page_text[:2000] + ("\n...\n" + page_text[-500:] if len(page_text) > 2000 else "")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": f"""
Read this webpage content and extract information about the entity.

URL: {page_url}
CONTENT: {text_sample}

Return ONLY valid JSON, no markdown:
{{
  "entity_type": "coach" or "club" or "neither",
  "name": "full name or organisation name, or empty string",
  "website": "website URL or empty string",
  "athlete_count_signal": "e.g. '~15 athletes', '40+ athletes', 'unknown'",
  "tools_mentioned": ["TrainingPeaks", "WhatsApp", "Garmin", "Strava", "Final Surge", "Excel"],
  "coaches": []
}}

Definitions:
- "coach": a personal endurance coach who coaches individual athletes (triathlon, running, cycling, swimming, etc.)
- "club": a sports club or association for endurance athletes (triathlon club, running club, cycling club, etc.)
- "neither": anything else (directory, shop, news site, etc.)
- athlete_count_signal: any mention of how many athletes or members they work with; use "unknown" if not mentioned
- tools_mentioned: only tools explicitly named on the page; empty list if none found
- coaches: only populated when entity_type is "club" — list of named individuals found on the page (max 3), each as {{"name": "...", "role": "head coach / founder / coach"}}. Empty list if entity_type is not "club" or no individuals are named.
"""}]
    )
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    parsed = json.loads(text)
    if isinstance(parsed, list):
        parsed = parsed[0] if parsed else {}
    return parsed


def research_coach(page) -> list:
    page_text = page.text or ""
    page_url  = page.url or ""

    info = extract_coach_info(page_text, page_url)

    entity_type = info.get("entity_type", "neither")
    if entity_type == "neither" or not info.get("name"):
        return []

    name    = info.get("name", "")
    website = info.get("website", page_url)
    contact = find_contact_info(page_text, website)

    shared = {
        "athlete_count_signal": info.get("athlete_count_signal", "unknown"),
        "tools_mentioned":      info.get("tools_mentioned", []),
        "website":              website,
        "website_text":         page_text[:3000],
        "channel":              contact["channel"],
        "instagram_url":        contact["instagram_url"],
        "linkedin_url":         contact["linkedin_url"],
        "phone":                contact["phone"],
        "email":                contact["email"],
    }

    if entity_type == "club":
        named_coaches = info.get("coaches", [])
        if not named_coaches:
            return []
        return [
            {"name": c["name"], "role": c.get("role", ""), "club_name": name,
             "entity_type": "coach", **shared}
            for c in named_coaches[:3]
        ]

    return [{"name": name, "entity_type": "coach", **shared}]


# ── Phase 4: Message Drafting ──────────────────────────────────────────────────

def _writing_rules() -> str:
    from datetime import date
    today = date.today().strftime("%B %d, %Y")
    return f"""
WRITING RULES — STRICT:
- Never use an em dash (—) anywhere
- Never start with "Noticed", "I noticed", "I came across", "I stumbled upon"
- Never use: seamlessly, leverage, revolutionize, game-changer, cutting-edge, innovative,
  transform, elevate, streamline, coaches like you, stands out, empower, unlock,
  data points, juggling, valuable, insights, holistic, journey, dedicated, passionate,
  efficiency, productivity, optimize, scale your business, game changer
- Never say augo replaces TrainingPeaks — it sits next to TP, not instead of it
- Never call augo an "AI coach" — it empowers the human coach, it does not replace them
- Bruna is a runner and running coach herself — write peer to peer, not sales pitch
- No corporate language, no AI-sounding phrases
- Write like a real person, not a marketer
- Today is {today}. Never reference a specific race, event, or date that has already passed.
  Only use timeless facts: who they coach, their approach, their results history, ongoing traits.
"""

def _call_claude(prompt: str) -> str:
    client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text

def _clean(text: str) -> str:
    return text.replace("—", "-").replace("–", "-")

def draft_dm(coach: dict) -> dict:
    is_club = coach.get("entity_type") == "club"
    recipient_desc = "sports club" if is_club else "coach"
    athlete_count  = coach.get("athlete_count_signal", "unknown")
    tools          = coach.get("tools_mentioned", [])
    tools_str      = ", ".join(tools) if tools else "not mentioned"

    if is_club:
        pain_context = (
            f"This is a sports club (athlete count: {athlete_count}). "
            "Clubs typically deal with fragmented communication across many members, "
            "coaches juggling WhatsApp groups, and no single view of who needs attention."
        )
        cta_examples = (
            "Worth 30 minutes to show you what that looks like for a club your size?"
            " / "
            "Happy to jump on a quick call this week if it sounds relevant?"
        )
    else:
        pain_context = (
            f"This coach has approximately {athlete_count} athletes. "
            f"Tools visible on their site: {tools_str}. "
            "Coaches at this scale typically spend 4-10h/week switching between WhatsApp, "
            "TrainingPeaks, and spreadsheets just to know who needs attention — "
            "and they've often quietly capped their roster to protect quality."
        )
        cta_examples = (
            "Worth 30 minutes to show you what that looks like in practice?"
            " / "
            "Happy to jump on a quick call this week if it sounds relevant?"
        )

    channel = coach.get("channel", "instagram")
    if channel == "website":
        format_label   = "contact form message"
        format_note    = "This will be sent via a website contact form — slightly more formal than a DM, but still short and conversational. No subject line needed."
        format_rules   = (
            "- Sentence 1: a genuine, specific observation about them. Must name something real. NOT a compliment.\n"
            "- Sentence 2: who Bruna is + core augo value framed around their situation. Do NOT list features.\n"
            f"- Sentence 3: ask for 30 minutes of their time / a quick call. Examples: {cta_examples}\n"
            "- Sign off with just 'Bruna' on a new line. 3 sentences + sign-off only."
        )
    elif channel == "linkedin":
        format_label   = "cold LinkedIn message"
        format_note    = "This will be sent as a LinkedIn direct message — slightly more professional tone than Instagram but still concise and personal."
        format_rules   = (
            "- Sentence 1: a genuine, specific observation. Must name something real. NOT a compliment.\n"
            "- Sentence 2: who Bruna is + core augo value framed around their situation. Do NOT list features.\n"
            f"- Sentence 3: ask for 30 minutes of their time / a quick call. Examples: {cta_examples}\n"
            "- No sign-off. 3 sentences only."
        )
    else:
        format_label   = "cold Instagram DM"
        format_note    = "This will be sent as an Instagram direct message — keep it very short and conversational."
        format_rules   = (
            f"- Sentence 1: a genuine, specific observation. Must name something real (a person, place, number, event). NOT a compliment.\n"
            f"- Sentence 2: one short sentence — who Bruna is + the core augo value framed around their specific situation (athlete count, tools, admin pain). Do NOT list features.\n"
            f"- Sentence 3: ask for 30 minutes of their time / a quick call. Examples: {cta_examples}\n"
            f"- No sign-off. 3 sentences only."
        )

    prompt = f"""
You are writing a {format_label} on behalf of {SENDER_NAME}, {SENDER_ROLE}.
{format_note}

WHAT AUGO DOES:
{AUGO_PITCH_SHORT}

KEY POSITIONING:
- augo tells coaches WHICH athlete needs attention right now — that's the core value
- It sits next to TrainingPeaks, it does NOT replace it
- It is NOT an AI coach — it empowers the human coach
- The problem it solves: coaches cap at 5-30 athletes to protect quality because admin eats their time

CONTEXT ABOUT THIS {recipient_desc.upper()}:
Name: {coach['name']}
{pain_context}

WEBSITE CONTENT:
{coach['website_text'][:2000] if coach['website_text'] else 'No website content available.'}

INSTRUCTIONS:
1. Read the website content carefully
2. Pick one specific, concrete detail — a real result, athlete name, race, place, or a choice the {recipient_desc} made
3. Write a {format_label} from {SENDER_NAME}

MESSAGE RULES:
{format_rules}

BAD sentence 2: "I'm Bruna, we help coaches bring everything into one place."
GOOD sentence 2 (solo coach ~20 athletes): "I'm Bruna, I'm building something that tells coaches which of their 20 athletes needs attention that day — without opening five different apps."
GOOD sentence 2 (coach at ceiling): "I'm Bruna, I'm building a tool for coaches who've quietly stopped taking new athletes because the admin doesn't leave room."

{_writing_rules()}

Return ONLY valid JSON, no markdown:
{{
  "hook_type": "athlete_achievement | their_race | content_published | coaching_philosophy | club_event | fallback",
  "hook_text": "the specific opening sentence used",
  "research_notes": "2-line summary: who they are and what makes them distinctive",
  "dm_message": "the full {format_label} text"
}}
"""
    result = json.loads(_call_claude(prompt))
    if isinstance(result, list):
        result = result[0]
    result["dm_message"] = _clean(result["dm_message"])
    return result


def draft_email(coach: dict) -> dict:
    is_club = coach.get("entity_type") == "club"
    recipient_desc = "sports club" if is_club else "coach"
    athlete_count  = coach.get("athlete_count_signal", "unknown")
    tools          = coach.get("tools_mentioned", [])
    tools_str      = ", ".join(tools) if tools else "not mentioned"

    if is_club:
        pain_context = (
            f"This is a sports club (member count: {athlete_count}). "
            "Club pain points: fragmented communication across many coaches and members, "
            "no single view of who needs a check-in, and coaches mixing personal and club WhatsApp."
        )
        cta_ask = "30 minutes to learn how your coaching team currently handles athlete communication"
    else:
        pain_context = (
            f"This coach has approximately {athlete_count} athletes. "
            f"Tools visible on their site: {tools_str}. "
            "Coaches at this scale typically spend 4-10h/week on admin across 5-7 disconnected tools. "
            "Many have quietly capped their roster because more athletes means more admin, not more income."
        )
        cta_ask = "30 minutes to understand how you currently manage everything across your athletes"

    prompt = f"""
You are writing a cold outreach email on behalf of {SENDER_NAME}, {SENDER_ROLE}.

WHAT AUGO DOES:
{AUGO_PITCH_FULL}

KEY POSITIONING:
- The core problem augo solves: coaches cap at 5-30 athletes to protect quality because admin overwhelms them
- augo tells coaches WHICH athlete needs attention right now — replacing the WhatsApp-TP-memory juggle
- It is NOT an AI coach and does NOT replace TrainingPeaks
- Bruna is a runner and running coach herself — this is peer to peer

CONTEXT ABOUT THIS {recipient_desc.upper()}:
Name: {coach['name']}
{pain_context}

WEBSITE CONTENT:
{coach['website_text'][:2000] if coach['website_text'] else 'No website content available.'}

INSTRUCTIONS:
1. Read the website content carefully
2. Pick one specific, concrete detail — a real result, athlete name, race, event, or decision the {recipient_desc} made
3. Write a short cold email from {SENDER_NAME}

EMAIL RULES:
- Subject: 4-6 words, reference something specific from their site
- Opening: one sentence using the specific concrete detail (not a compliment)
- Body paragraph: briefly introduce Bruna + frame augo around the pain this {recipient_desc} likely feels based on their situation. Don't list features. Reference their approximate scale or tools if you know them.
- CTA: ask for {cta_ask}
- Sign-off: "{SENDER_NAME}" only
- Total body: under 120 words
- Tone: peer-to-peer, not a sales pitch

{_writing_rules()}

Return ONLY valid JSON, no markdown:
{{
  "hook_type": "athlete_achievement | their_race | content_published | coaching_philosophy | club_event | fallback",
  "hook_text": "the specific opening sentence used",
  "research_notes": "2-line summary: who they are and what makes them distinctive",
  "subject": "the subject line",
  "message": "the full email body"
}}
"""
    result = json.loads(_call_claude(prompt))
    if isinstance(result, list):
        result = result[0]
    result["message"] = _clean(result["message"])
    return result


# ── Fake data for --test mode ──────────────────────────────────────────────────

FAKE_COACHES = [
    {
        "name":                 "Jan de Vries",
        "entity_type":          "coach",
        "athlete_count_signal": "~15 athletes",
        "tools_mentioned":      ["TrainingPeaks", "WhatsApp"],
        "website":              "https://jancoaching.nl",
        "channel":              "instagram",
        "instagram_url":        "https://instagram.com/jandevries_tri",
        "linkedin_url":         "",
        "phone":                "",
        "email":                "",
        "website_text": (
            "Jan de Vries is a triathlon coach based in Amsterdam. "
            "This month his athlete Emma Bakker finished Ironman 70.3 Maastricht in 4:32, a new personal best. "
            "Jan has been coaching Emma for two years, building her race-day execution from scratch. "
            "He coaches 15 athletes individually, all focused on the 70.3 distance. "
            "Former competitive swimmer who transitioned to triathlon coaching in 2012. "
            "Training plans are delivered through TrainingPeaks. Athlete communication via WhatsApp."
        ),
    },
    {
        "name":                 "Zürich Triathlon Club",
        "entity_type":          "club",
        "athlete_count_signal": "200+ members",
        "tools_mentioned":      ["WhatsApp", "Strava"],
        "website":              "https://zuerich-tri.ch",
        "channel":              "email",
        "instagram_url":        "",
        "linkedin_url":         "",
        "phone":                "",
        "email":                "info@zuerich-tri.ch",
        "website_text": (
            "Zürich Triathlon Club was founded in 1998 and has over 200 active members. "
            "40 members competed at Zürich Triathlon last summer, with 8 podium finishes. "
            "The club runs weekly coached swim, bike, and run sessions and an annual training camp in Mallorca. "
            "Coaches coordinate sessions via WhatsApp groups. Members track training on Strava. "
            "Membership is open to all levels from beginner to elite."
        ),
    },
]


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    from attio import get_existing_domains, push_coach as attio_push

    print("\n=== augo Coach Outreach Pipeline ===\n")

    test_mode = "--test"      in sys.argv
    from_file = "--from-file" in sys.argv
    dry_run   = "--dry-run"   in sys.argv

    if test_mode:
        print("TEST MODE — using fake coach data, no Exa.\n")
        coaches_data = FAKE_COACHES
        query = "test"

    elif from_file:
        if not os.path.exists("coaches.json"):
            print("coaches.json not found — run with --fetch first.")
            return
        with open("coaches.json") as f:
            coaches_data = json.load(f)
        query = "from_file"
        print(f"Loaded {len(coaches_data)} coaches from coaches.json\n")

    else:
        query = input("What do you want to search for?\n> ").strip()
        while not query:
            query = input("Please enter a search query\n> ").strip()
        limit_input = input("\nHow many? (max 20, default: 10)\n> ").strip()
        try:
            limit = min(int(limit_input), 20) if limit_input else 10
        except ValueError:
            limit = 10

        country_code = country_from_query(query)

        print("\nFetching existing coaches from Attio...")
        existing_domains = get_existing_domains()
        if existing_domains:
            print(f"  Excluding {len(existing_domains)} already-found domains from Exa search\n")

        candidates   = find_coaches(query, limit, extra_exclude=existing_domains)
        coaches_data = []
        seen_root_domains = set()
        for candidate in candidates:
            if len(coaches_data) >= limit:
                break
            from urllib.parse import urlparse
            root = urlparse(candidate.url).netloc.replace("www.", "").lower()
            if root in seen_root_domains:
                continue
            seen_root_domains.add(root)
            page = fetch_page_content(candidate)
            if not page:
                continue
            for coach in research_coach(page):
                coach["country_code"] = country_code
                coaches_data.append(coach)
                if len(coaches_data) >= limit:
                    break

    print(f"Drafting messages...\n")
    pushed = 0

    for i, coach_data in enumerate(coaches_data, 1):
        print(f"  [{i}/{len(coaches_data)}] {coach_data['name']}...", end=" ", flush=True)

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
            print(f"✗ drafting failed: {e}")
            continue

        research_notes = draft.get("research_notes", "")

        socials = []
        if coach_data.get("instagram_url"): socials.append(f"Instagram: {coach_data['instagram_url']}")
        if coach_data.get("email"):         socials.append(f"Email:     {coach_data['email']}")
        if coach_data.get("phone"):         socials.append(f"Phone:     {coach_data['phone']}")
        if coach_data.get("linkedin_url"):  socials.append(f"LinkedIn:  {coach_data['linkedin_url']}")
        if coach_data.get("website"):       socials.append(f"Website:   {coach_data['website']}")

        color = CHANNEL_COLOR.get(channel, RESET)
        print(f"✓")
        print(f"\n{'='*60}")
        print(f"  {BOLD}{coach_data['name']}{RESET}")
        print(f"  Channel: {color}{channel}{RESET}")
        for s in socials:
            print(f"  {s}")
        if research_notes:
            print(f"\n  About:   {research_notes}")
        if subject:
            print(f"\n  Subject: {subject}")
        print(f"\n  Message:\n  {message.replace(chr(10), chr(10) + '  ')}")
        print(f"{'='*60}\n")

        if dry_run:
            print(f"  → Dry run — skipping Attio push")
        else:
            ok = attio_push(coach_data, message, subject=subject, research_notes=research_notes)
            if ok:
                pushed += 1
                print(f"  → Pushed to Attio Sales Pipeline")
            else:
                print(f"  → Attio push failed")

    if dry_run:
        print(f"\nDone — {len(coaches_data)} coaches found (dry run, nothing pushed to Attio)")
    else:
        print(f"\nDone — {pushed}/{len(coaches_data)} coaches pushed to Attio")


if __name__ == "__main__":
    main()
