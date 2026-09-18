"""Recently watched Jellyfin items — a direct, structured API query.

Not a Qdrant search: exact sort by UserData.LastPlayedDate, always
current since it hits the live Jellyfin API. See ADR-0008.

Usage:
    sops exec-env ../../secrets.enc.env \\
      'sops exec-env secrets.enc.env "uv run recent.py"'
    sops exec-env ../../secrets.enc.env \\
      'sops exec-env secrets.enc.env "uv run recent.py --type Series --limit 10"'
"""

import argparse

import jellyfin_client
from textify import clean_title


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", default="Movie", choices=["Movie", "Series"])
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    items = jellyfin_client.get_items(args.type)
    watched = [i for i in items if i.get("UserData", {}).get("LastPlayedDate")]
    watched.sort(key=lambda i: i["UserData"]["LastPlayedDate"], reverse=True)

    for item in watched[: args.limit]:
        last_played = item["UserData"]["LastPlayedDate"]
        title = clean_title(item["Name"])
        year = item.get("ProductionYear")
        year_part = f" ({year})" if year else ""
        print(f"{last_played}  {title}{year_part}")


if __name__ == "__main__":
    main()
