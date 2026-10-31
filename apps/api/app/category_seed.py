"""Seed the canonical Fynd competition categories."""

import argparse
import json

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Category

CATEGORIES = (
    ("AI & Machine Learning", "ai-machine-learning"),
    ("Data Science & Analytics", "data-science"),
    ("Web & App Development", "web-development"),
    ("Cybersecurity", "cybersecurity"),
    ("Blockchain & Web3", "blockchain-web3"),
    ("IoT & Robotics", "iot-robotics"),
    ("Game Development", "game-development"),
    ("Open Source", "open-source"),
    ("Healthcare & Biotech", "healthcare-biotech"),
    ("Climate & Sustainability", "climate-sustainability"),
    ("Fintech", "fintech"),
    ("Hardware & Electronics", "hardware-electronics"),
)


def seed_categories(session: Session) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {
        "created": [],
        "already_existing": [],
        "conflicts": [],
    }
    for name, slug in CATEGORIES:
        category = session.scalar(
            select(Category).where(
                or_(
                    Category.slug == slug,
                    func.lower(Category.name) == name.lower(),
                )
            )
        )
        entry = {"name": name, "slug": slug}
        if category is not None:
            if category.name == name and category.slug == slug:
                result["already_existing"].append(entry)
            else:
                result["conflicts"].append(
                    {
                        **entry,
                        "existing_name": category.name,
                        "existing_slug": category.slug,
                    }
                )
            continue

        session.add(Category(name=name, slug=slug))
        session.flush()
        result["created"].append(entry)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Fynd's canonical competition categories")
    parser.parse_args()
    with SessionLocal() as session, session.begin():
        result = seed_categories(session)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
