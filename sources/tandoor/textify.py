"""Turn a Tandoor recipe (title, description, steps, ingredients) into one
plain-text blob for embedding — same idea as sources/obsidian's recipes,
just built from Tandoor's structured API instead of a markdown file.
"""


def _fmt_amount(amount: float) -> str:
    return str(int(amount)) if amount == int(amount) else str(amount)


def _fmt_ingredient(ing: dict) -> str:
    amount = ing.get("amount") or 0
    unit = (ing.get("unit") or {}).get("name", "")
    food = (ing.get("food") or {}).get("name", "")
    parts = [p for p in (_fmt_amount(amount) if amount else "", unit, food) if p]
    line = " ".join(parts)
    if note := ing.get("note"):
        line += f" ({note})"
    return line


def textify(recipe: dict) -> str:
    lines = [recipe["name"]]

    if description := recipe.get("description"):
        lines.append(description)

    if keywords := [k["name"] for k in recipe.get("keywords", [])]:
        lines.append("Теги: " + ", ".join(keywords))

    meta = []
    if servings := recipe.get("servings"):
        meta.append(f"Порцій: {servings}")
    if working_time := recipe.get("working_time"):
        meta.append(f"Час приготування: {working_time} хв")
    if waiting_time := recipe.get("waiting_time"):
        meta.append(f"час очікування: {waiting_time} хв")
    if meta:
        lines.append(", ".join(meta))

    for i, step in enumerate(recipe.get("steps", []), start=1):
        lines.append(f"\nКрок {i}: {step.get('name') or ''}".strip())
        ingredients = [_fmt_ingredient(ing) for ing in step.get("ingredients", [])]
        if ingredients:
            lines.append("Інгредієнти: " + ", ".join(ingredients))
        if instruction := step.get("instruction"):
            lines.append(instruction)

    return "\n".join(lines)
