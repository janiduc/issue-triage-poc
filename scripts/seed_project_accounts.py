"""Add the Harbour Bookings project and separate demo accounts for each repository.

Usage (development only, against the database named in .env):
    python scripts/seed_project_accounts.py

Safe to run more than once. It
- creates the "harbour" project (GitHub janiduc/harbour-bookings, token in GITHUB_TOKEN_HARBOUR)
  if it does not exist yet;
- creates one triage lead, developers and one reporter per repository, all with the same demo
  password as the other demo accounts (see the README), and gives each a role in ONE project only;
- maps the developers to GitHub collaborators of their repository.
The first project must be the bluefin-shop project created from .env.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from triage import config  # noqa: E402

config.load_env_file()

from triage import db  # noqa: E402

HARBOUR_MODULES = ["bookings", "payments", "notifications", "other"]

# username, display name, project slug, role, modules, GitHub login
ACCOUNTS = [
    ("bluefin.lead", "Bluefin lead", "bluefin", "triage_lead", [], None),
    ("bluefin.dev.payments", "Bluefin developer, payments and reporting", "bluefin", "developer",
     ["payments", "reporting"], "lakmini77"),
    ("bluefin.dev.platform", "Bluefin developer, platform and API", "bluefin", "developer",
     ["authentication", "api", "performance"], "ImalAyodya"),
    ("bluefin.dev.frontend", "Bluefin developer, front end", "bluefin", "developer",
     ["user_interface", "other"], "chamithusithmaka"),
    ("bluefin.reporter", "Bluefin store owner (reporter)", "bluefin", "reporter", [], None),
    ("harbour.lead", "Harbour lead", "harbour", "triage_lead", [], None),
    ("harbour.dev.bookings", "Harbour developer, bookings", "harbour", "developer",
     ["bookings", "other"], "Maleesha-Shavindi"),
    ("harbour.dev.payments", "Harbour developer, payments and notifications", "harbour", "developer",
     ["payments", "notifications"], "a0alexander"),
    ("harbour.reporter", "Harbour front desk (reporter)", "harbour", "reporter", [], None),
]


def main():
    db.init_db()
    projects = db.list_projects()
    first = projects[0]
    harbour = db.get_project_by_slug("harbour")
    if not harbour:
        harbour_id = db.create_project("harbour", "Harbour Bookings", "github", HARBOUR_MODULES,
                                       repo="janiduc/harbour-bookings", token_env="GITHUB_TOKEN_HARBOUR")
        print("created project harbour")
    else:
        harbour_id = harbour["id"]
    project_ids = {"bluefin": first["id"], "harbour": harbour_id}
    for username, name, slug, role, modules, login in ACCOUNTS:
        user = db.get_user_by_username(username)
        if not user:
            user_id = db.create_user(username, name, role, db.DEMO_PASSWORD, modules if slug == "bluefin" else [])
            print("created", username)
        else:
            user_id = user["id"]
        # exactly one project: the one this account belongs to
        for other_slug, pid in project_ids.items():
            if other_slug != slug:
                db.remove_membership(pid, user_id)
        db.set_membership(project_ids[slug], user_id, role, modules)
        if login:
            db.update_user(user_id, github_login=login)
    print(f"done: {len(ACCOUNTS)} accounts across {first['name']} and Harbour Bookings")


if __name__ == "__main__":
    main()
