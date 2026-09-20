"""One-time migration: household inventory notes (Obsidian) -> Grocy.

Per ADR-0032 Grocy becomes the primary inventory; the notes stay untouched.
Free-text lines ("Рис - 2х0.8 кг", "Кукурудза - 10 банок", "Крупа гречка")
are parsed into name / amount / unit. Lines that can't be parsed to an
amount are NOT guessed: the product is created with no stock and listed in
the report for the user to decide.

Dry run by default; writes only with --apply. Skips products whose name
already exists (case-insensitive), so it is safe to re-run.

    sops exec-env ../secrets.enc.env 'sops --config /dev/null exec-env secrets.enc.env \\
      "uv run python migrate_grocy_from_obsidian.py [--apply]"'
"""

import argparse
import pathlib
import re

NOTES = pathlib.Path("/home/punka/obsidian/Домашнє")
# note file -> (Grocy location, product group)
SOURCES = {
    "Продукти кладова.md": ("Кладова", "Продукти"),
    "Продукти кухня.md": ("Кухня", "Продукти"),
    "Господарські товари.md": ("Господарські товари", "Господарські товари"),
}
# unit spelling in the notes -> Grocy stock unit name
UNITS = {
    "кг": "кг", "г": "г", "гр": "г", "грам": "г", "грамів": "г", "грамм": "г",
    "л": "л", "мл": "мл",
    "банок": "банка", "банка": "банка", "банки": "банка", "банку": "банка",
    "шт": "шт", "штук": "шт", "штука": "шт", "штуки": "шт",
    "уп": "уп", "упаковка": "уп", "упаковки": "уп",
    "рулонів": "рулон", "рулони": "рулон", "рулон": "рулон",
    "листів": "аркуш", "аркушів": "аркуш",
}
_UNIT_ALT = "|".join(sorted(UNITS, key=len, reverse=True))
_NUM = r"\d+(?:[.,]\d+)?"
_MULT_RE = re.compile(rf"({_NUM})\s*[xх×]\s*({_NUM})\s*({_UNIT_ALT})\b", re.IGNORECASE)   # 2х0.8 кг
_QTY_RE = re.compile(rf"({_NUM})\s*({_UNIT_ALT})\b", re.IGNORECASE)                       # 10 банок


def _f(s: str) -> float:
    return float(s.replace(",", "."))


def split_lines(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        # the notes contain "…400 грамів13. Дріжджі сухі": two items glued together
        out += re.split(r"(?<=\D)(?=\d{1,2}\.\s)", line)
    return [re.sub(r"^\s*\d+\.\s*", "", p).strip() for p in out if re.sub(r"^\s*\d+\.\s*", "", p).strip()]


def parse(line: str) -> dict:
    name, sep, rest = line.partition(" - ")
    if not sep:
        # "Нут 200 грамів": no dash, the amount sits at the end of the name
        m = _QTY_RE.search(line)
        if m and not line[m.end():].strip():
            name, rest = line[:m.start()].strip(), line[m.start():]
    name, rest = name.strip(), rest.strip()
    amount = unit = None
    m = _MULT_RE.search(rest)
    if m:
        amount, unit = _f(m[1]) * _f(m[2]), UNITS[m[3].lower()]
        before = rest[:m.start()]
    elif (m := _QTY_RE.search(rest)):
        amount, unit = _f(m[1]), UNITS[m[2].lower()]
        before = rest[:m.start()]
    else:
        before = ""
    # "туалетний папір - zewa, 24 рулони": text before the quantity is the brand
    brand = before.strip(" ,;")
    if brand:
        name = f"{name} {brand}"
    return {"name": name, "amount": round(amount, 3) if amount is not None else None, "unit": unit, "raw": line}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    items = []
    for fname, (loc, group) in SOURCES.items():
        for line in split_lines((NOTES / fname).read_text(encoding="utf-8")):
            items.append({**parse(line), "location": loc, "group": group})

    print(f"{len(items)} items parsed from {len(SOURCES)} notes\n")
    for it in items:
        amt = f"{it['amount']:g} {it['unit']}" if it["amount"] is not None else "— БЕЗ КІЛЬКОСТІ"
        print(f"  [{it['location'][:9]:9}] {it['name'][:44]:44} {amt:22} <- {it['raw'][:38]!r}")
    unknown = [i for i in items if i["amount"] is None]
    print(f"\nwithout an amount (will be created with no stock): {len(unknown)}")
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    import grocy_client as g
    units = {u["name"].lower(): u["id"] for u in g.objects("quantity_units")}
    locs = {l["name"].lower(): l["id"] for l in g.objects("locations")}
    groups = {x["name"].lower(): x["id"] for x in g.objects("product_groups")}
    have = {p["name"].lower() for p in g.objects("products")}
    for name in sorted({i["unit"] for i in items if i["unit"]} | {"шт"}):
        if name.lower() not in units:
            units[name.lower()] = g.create("quantity_units", {"name": name, "name_plural": name})
    for loc in {i["location"] for i in items}:
        if loc.lower() not in locs:
            locs[loc.lower()] = g.create("locations", {"name": loc})
    for grp in {i["group"] for i in items}:
        if grp.lower() not in groups:
            groups[grp.lower()] = g.create("product_groups", {"name": grp})

    created = skipped = stocked = 0
    for it in items:
        if it["name"].lower() in have:
            skipped += 1
            continue
        qu = units[(it["unit"] or "шт").lower()]
        pid = g.create("products", {
            "name": it["name"], "location_id": locs[it["location"].lower()],
            "product_group_id": groups[it["group"].lower()],
            "qu_id_stock": qu, "qu_id_purchase": qu, "qu_id_consume": qu, "qu_id_price": qu,
            "description": f"З нотатки Obsidian: {it['raw']}",
        })
        created += 1
        if it["amount"]:
            g.set_stock(pid, it["amount"], locs[it["location"].lower()], note="початковий запас з нотатки")
            stocked += 1
    print(f"\nAPPLIED: {created} products created, {stocked} with stock, {skipped} skipped (already exist)")


if __name__ == "__main__":
    main()
