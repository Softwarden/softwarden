"""Recreate the Playwright database from scratch: `python e2e_db.py` drops and creates it.

It only ever touches a database whose name ends in `_e2e`, so a wrong POSTGRES_DB can't wipe the dev database.
Connection settings come from the same POSTGRES_* variables as settings.py.
"""

import os
import sys

import psycopg
from psycopg import sql

name = os.environ.get("POSTGRES_DB", "")
if not name.endswith("_e2e"):
    sys.exit(f"Refusing to recreate {name!r}: set POSTGRES_DB to a name ending in _e2e.")

with psycopg.connect(
    dbname="postgres",
    user=os.environ.get("POSTGRES_USER", "softwarden"),
    password=os.environ.get("POSTGRES_PASSWORD", "softwarden-dev"),
    host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
    port=os.environ.get("POSTGRES_PORT", "5434"),
    autocommit=True,
) as conn:
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
print(f"Recreated {name}")
