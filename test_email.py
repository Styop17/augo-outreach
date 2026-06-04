import json
from pipeline import find_email_exa, EXA_API_KEY
from exa_py import Exa

exa = Exa(EXA_API_KEY)

with open("coaches.json") as f:
    coaches = json.load(f)

for coach in coaches:
    name  = coach.get("name", "")
    email = coach.get("email", "")
    sport = coach.get("sport", "endurance")

    if email:
        print(f"{name}: already has email — {email}")
    else:
        found = find_email_exa(name, sport, exa)
        print(f"{name}: {found if found else 'no email found'}")
