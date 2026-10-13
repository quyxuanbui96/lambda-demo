#!/usr/bin/env python3
"""Explicit schema migration command; Lambda handlers never initialize schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateIndex, CreateTable, DropTable

from scripts.local_env import load_local_environment
from src.workflow.config import load_workflow_settings
from src.workflow.database import Database, metadata
from src.workflow.runtime import SecretCache, create_database


def export_sql() -> None:
    dialect = create_engine("postgresql+psycopg://").dialect
    directory = Path(__file__).resolve().parent.parent / "migrations"
    statements = [
        "\n".join(
            line.rstrip() for line in str(CreateTable(table).compile(dialect=dialect)).splitlines()
        ).strip()
        + ";"
        for table in metadata.sorted_tables
    ]
    statements += [
        str(CreateIndex(index).compile(dialect=dialect)) + ";"
        for table in metadata.sorted_tables
        for index in sorted(table.indexes, key=lambda i: str(i.name))
    ]
    (directory / "001_workflow.up.sql").write_text(
        "-- Initial workflow schema. Run explicitly with the DB/Infra team.\n\n"
        + "\n\n".join(statements)
        + "\n"
    )
    (directory / "001_workflow.down.sql").write_text(
        "-- Rollback deletes workflow data; take a backup before running.\n\n"
        + "\n\n".join(
            str(DropTable(table).compile(dialect=dialect)).strip() + ";"
            for table in reversed(metadata.sorted_tables)
        )
        + "\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-sql", action="store_true")
    parser.add_argument("--local-url")
    args = parser.parse_args()
    if args.export_sql:
        export_sql()
        return
    if args.local_url:
        if not args.local_url.startswith("sqlite"):
            parser.error("--local-url only accepts SQLite")
        database = Database.local(args.local_url)
    else:
        load_local_environment()
        settings = load_workflow_settings()
        cache = SecretCache(boto3.client("secretsmanager"), settings.secret_cache_seconds)
        database = create_database(settings, cache)
    metadata.create_all(database.engine)
    database.engine.dispose()
    print("Workflow schema migration applied")


if __name__ == "__main__":
    main()
