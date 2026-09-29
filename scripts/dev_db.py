"""Development database bootstrap.

Runs a real PostgreSQL 16 cluster (via the pgserver package) at ./pgdata and
writes DATABASE_URL / OPSHUB_TOKEN_KEY / OPSHUB_SECRET_KEY into .env if they
are missing. This is the local substitute for `docker compose up -d db` on
machines where Docker is unavailable. Same engine, same semantics: partial
unique indexes, triggers, and CHECK constraints all behave identically.

Refuses to run when OPSHUB_ENV=production: a stray run against a real
environment would hand the token key to a file.
"""

from __future__ import annotations

import base64
import os
import pathlib
import secrets

import pgserver

ROOT = pathlib.Path(__file__).resolve().parent.parent
PGDATA = ROOT / "pgdata"
ENV_FILE = ROOT / ".env"

GENERATED = ("DATABASE_URL", "OPSHUB_TOKEN_KEY", "OPSHUB_SECRET_KEY")


def main() -> None:
    if os.environ.get("OPSHUB_ENV", "").strip() == "production":
        raise SystemExit("dev_db.py: refusing, this is development tooling")

    PGDATA.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(PGDATA, cleanup_mode=None)
    uri = server.get_uri()

    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    values = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in lines
        if "=" in line and not line.strip().startswith("#")
    }

    additions: list[str] = []
    if not values.get("DATABASE_URL"):
        additions.append(f"DATABASE_URL={uri}")
    if not values.get("OPSHUB_TOKEN_KEY"):
        additions.append(f"OPSHUB_TOKEN_KEY={base64.b64encode(secrets.token_bytes(32)).decode()}")
    if not values.get("OPSHUB_SECRET_KEY"):
        additions.append(f"OPSHUB_SECRET_KEY={secrets.token_urlsafe(48)}")

    if additions:
        if ENV_FILE.exists() and ENV_FILE.read_text().strip() and not ENV_FILE.read_text().rstrip().endswith("\n\n"):
            ENV_FILE.write_text(ENV_FILE.read_text().rstrip() + "\n\n")
        with ENV_FILE.open("a") as fh:
            fh.write("\n".join(additions) + "\n")
        for name, value in (a.split("=", 1) for a in additions):
            values[name] = value

    print(f"postgres ready: {uri}")
    print("env: " + ", ".join(f"{n}=set" for n in GENERATED if values.get(n)))


if __name__ == "__main__":
    main()