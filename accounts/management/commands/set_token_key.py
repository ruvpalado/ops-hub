"""Generate an OPSHUB_TOKEN_KEY and (optionally) write it into .env.

The key is 32 random bytes, base64-encoded — the exact shape accounts.crypto
wants. With --write-env it is appended to .env for local development; without a
flag it only prints, for deployment scripts that want the value on stdout.

The running process still holds the old key until restart; the command says so
plainly, because a half-applied key is the one failure mode that would quietly
corrupt every stored token on the next write.
"""

from __future__ import annotations

import base64
import secrets

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Generate a fresh base64 OPSHUB_TOKEN_KEY, optionally writing it to .env"

    def add_arguments(self, parser):
        parser.add_argument("--write-env", action="store_true", help="Append the key to .env")
        parser.add_argument("--env-file", default=".env", help="Path of the .env file to write")

    def handle(self, *args, **options):
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        if not options["write_env"]:
            self.stdout.write(key)
            return
        path = options["env_file"]
        try:
            with open(path, "a") as fh:
                fh.write(f"OPSHUB_TOKEN_KEY={key}\n")
        except OSError as exc:
            raise CommandError(f"cannot write {path}: {exc}") from exc
        self.stdout.write(f"key written to {path}; restart the process so Django loads it")
