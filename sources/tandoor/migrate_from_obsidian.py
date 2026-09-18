"""One-time migration: copy every Obsidian recipe note into Tandoor.

Per ADR-0015: Tandoor becomes the primary (writable) recipe source;
Obsidian recipe notes are migrated here rather than kept as a second
writable source. Each note becomes one Tandoor recipe with a single step
holding the note's full body text — no attempt to parse ingredients into
Tandoor's structured food/unit/amount model (the notes' formatting is too
inconsistent across files to do that reliably; see ADR-0015 for why that
tradeoff was made deliberately). Frontmatter `tags` and a `техніка` field
(equipment/technique, present on some notes) both become Tandoor keywords
— technique tags are what a future equipment-aware recipe search can
filter on.

Usage (only needs the Tandoor secret, no Qdrant/embedding involved):
    sops exec-env secrets.enc.env 'uv run migrate_from_obsidian.py'

Safe to re-run: skips notes whose title already exists as a Tandoor
recipe name.
"""

import pathlib
import re

import yaml

import tandoor_client

RECIPES_DIR = pathlib.Path("/home/punka/obsidian/Домашнє/рецепти")

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def parse_note(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    raw_frontmatter, body = match.groups()
    try:
        frontmatter = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError:
        frontmatter = {}
    return frontmatter, body.strip()


def extract_keywords(frontmatter: dict) -> list[str]:
    tags = frontmatter.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    keywords = [str(t) for t in tags]
    if technique := frontmatter.get("техніка"):
        keywords.append(str(technique))
    return keywords


def main() -> None:
    existing = {r["name"] for r in tandoor_client.get_recipes()}

    files = sorted(RECIPES_DIR.glob("*.md"))
    print(f"{len(files)} Obsidian recipe notes found")

    migrated = 0
    for path in files:
        title = path.stem
        if title in existing:
            print(f"skip (already in Tandoor): {title}")
            continue

        frontmatter, body = parse_note(path.read_text(encoding="utf-8"))
        keywords = extract_keywords(frontmatter)

        payload = {
            "name": title,
            "description": frontmatter.get("source", "")[:512],
            "keywords": [{"name": k} for k in keywords],
            "steps": [{"name": "Рецепт", "instruction": body, "ingredients": []}],
        }

        recipe = tandoor_client.create_recipe(payload)
        print(f"migrated: {title} -> Tandoor id {recipe['id']}")
        migrated += 1

    print(f"\nMigrated {migrated} recipes into Tandoor")


if __name__ == "__main__":
    main()
