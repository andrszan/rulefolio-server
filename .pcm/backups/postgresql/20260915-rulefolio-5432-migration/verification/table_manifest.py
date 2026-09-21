#!/usr/bin/env python3
"""生成不包含业务行内容的 PostgreSQL 表计数与 SHA-256 摘要。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def run_psql(host: str, port: int, user: str, database: str, sql: str) -> bytes:
    command = [
        "/opt/homebrew/opt/postgresql@14/bin/psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        database,
        "-At",
        "-c",
        sql,
    ]
    return subprocess.run(command, check=True, capture_output=True).stdout


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def scalar(host: str, port: int, user: str, database: str, sql: str) -> str:
    return run_psql(host, port, user, database, sql).decode().strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--user", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    table_rows = scalar(
        args.host,
        args.port,
        args.user,
        args.database,
        """
        SELECT c.relname || E'\\t' ||
               COALESCE(string_agg(a.attname, ',' ORDER BY k.ordinality), '')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_index i ON i.indrelid = c.oid AND i.indisprimary
        LEFT JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
          ON true
        LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        GROUP BY c.relname
        ORDER BY c.relname
        """,
    ).splitlines()

    tables: list[dict[str, object]] = []
    for row in table_rows:
        table, key_text = row.split("\t", 1)
        keys = [key for key in key_text.split(",") if key]
        if not keys:
            key_text = scalar(
                args.host,
                args.port,
                args.user,
                args.database,
                f"""
                SELECT string_agg(attname, ',' ORDER BY attnum)
                FROM pg_attribute
                WHERE attrelid = 'public.{quote_ident(table)}'::regclass
                  AND attnum > 0 AND NOT attisdropped
                """,
            )
            keys = key_text.split(",")
        order_by = ", ".join(quote_ident(key) for key in keys)
        table_ident = quote_ident(table)
        data = run_psql(
            args.host,
            args.port,
            args.user,
            args.database,
            f"COPY (SELECT * FROM public.{table_ident} ORDER BY {order_by}) TO STDOUT",
        )
        count = int(
            scalar(
                args.host,
                args.port,
                args.user,
                args.database,
                f"SELECT count(*) FROM public.{table_ident}",
            )
        )
        tables.append(
            {
                "table": table,
                "primary_key": keys,
                "row_count": count,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    metadata_sql = """
    SELECT json_build_object(
      'version', current_setting('server_version'),
      'encoding', pg_encoding_to_char(d.encoding),
      'collate', d.datcollate,
      'ctype', d.datctype,
      'alembic_revision', (SELECT version_num FROM public.alembic_version),
      'extensions', (SELECT json_agg(extname ORDER BY extname) FROM pg_extension),
      'rls', (
        SELECT json_agg(json_build_object(
          'table', c.relname,
          'enabled', c.relrowsecurity,
          'forced', c.relforcerowsecurity
        ) ORDER BY c.relname)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
      ),
      'policies', (
        SELECT json_agg(json_build_object(
          'schema', schemaname,
          'table', tablename,
          'name', policyname,
          'permissive', permissive,
          'roles', roles,
          'command', cmd,
          'using', qual,
          'check', with_check
        ) ORDER BY tablename, policyname)
        FROM pg_policies WHERE schemaname = 'public'
      )
    )
    FROM pg_database d WHERE d.datname = current_database()
    """
    metadata = json.loads(scalar(args.host, args.port, args.user, args.database, metadata_sql))
    manifest = {"database": args.database, "metadata": metadata, "tables": tables}
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    args.output.chmod(0o600)


if __name__ == "__main__":
    main()
