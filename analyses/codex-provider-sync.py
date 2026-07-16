#!/usr/bin/env python3
"""
Sync Codex thread records from one model_provider to another.

Default mode is dry-run. Add --apply to modify state_5.sqlite and rollout JSONL
files. The script backs up the SQLite DB and every touched rollout file before
writing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import shutil
import sqlite3
import sys
from typing import Any


def normalize_windows_path(value: str | None) -> pathlib.Path | None:
    if not value:
        return None
    text = str(value)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    # Some old paths can become "\C:\..." after an unsafe prefix strip.
    if len(text) >= 4 and text[0] == "\\" and text[2] == ":" and text[3] == "\\":
        text = text[1:]
    return pathlib.Path(text)


def connect_db(path: pathlib.Path, readonly: bool) -> sqlite3.Connection:
    if readonly:
        uri = "file:" + str(path).replace("\\", "/") + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    return sqlite3.connect(str(path))


def load_threads(
    con: sqlite3.Connection,
    from_provider: str,
    cwd: str | None,
) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    if cwd:
        return list(
            con.execute(
                """
                select id, model_provider, cwd, title, rollout_path
                from threads
                where model_provider = ? and lower(cwd) in (?, ?)
                order by updated_at desc
                """,
                (from_provider, cwd.lower(), ("\\\\?\\" + cwd).lower()),
            )
        )
    return list(
        con.execute(
            """
            select id, model_provider, cwd, title, rollout_path
            from threads
            where model_provider = ?
            order by updated_at desc
            """,
            (from_provider,),
        )
    )


def backup_inputs(
    codex_home: pathlib.Path,
    state_db: pathlib.Path,
    rows: list[sqlite3.Row],
    label: str,
) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = codex_home.parent / f".codex-provider-sync-backup-{stamp}-{label}"
    backup_dir.mkdir(parents=True, exist_ok=False)

    src = connect_db(state_db, readonly=True)
    dst = sqlite3.connect(str(backup_dir / "state_5.sqlite"))
    src.backup(dst)
    dst.close()
    src.close()

    manifest: list[dict[str, Any]] = []
    for row in rows:
        path = normalize_windows_path(row["rollout_path"])
        exists = bool(path and path.exists())
        rel: pathlib.Path | None = None
        if exists and path:
            try:
                rel = path.relative_to(codex_home)
            except ValueError:
                rel = pathlib.Path("external") / path.drive.replace(":", "") / pathlib.Path(
                    *path.parts[1:]
                )
            target = backup_dir / "files" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        manifest.append(
            {
                "id": row["id"],
                "cwd": row["cwd"],
                "title": row["title"],
                "rollout_path": str(path) if path else None,
                "exists": exists,
                "backup_relative": str(rel) if rel else None,
            }
        )

    (backup_dir / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": stamp,
                "codex_home": str(codex_home),
                "thread_count": len(rows),
                "files": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return backup_dir


def update_jsonl_provider(path: pathlib.Path, from_provider: str, to_provider: str) -> int:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    output: list[str] = []
    changed = 0

    for line in lines:
        newline = ""
        body = line
        if line.endswith("\r\n"):
            body, newline = line[:-2], "\r\n"
        elif line.endswith("\n"):
            body, newline = line[:-1], "\n"

        if body:
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                output.append(line)
                continue

            payload = obj.get("payload") if isinstance(obj, dict) else None
            if (
                isinstance(obj, dict)
                and obj.get("type") == "session_meta"
                and isinstance(payload, dict)
                and payload.get("model_provider") == from_provider
            ):
                payload["model_provider"] = to_provider
                body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                changed += 1

        output.append(body + newline)

    if changed:
        path.write_text("".join(output), encoding="utf-8", newline="")
    return changed


def read_jsonl_providers(path: pathlib.Path) -> set[str | None]:
    providers: set[str | None] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = obj.get("payload") if isinstance(obj, dict) else None
        if obj.get("type") == "session_meta" and isinstance(payload, dict):
            providers.add(payload.get("model_provider"))
    return providers


def verify(con: sqlite3.Connection) -> list[tuple[str, str, Any]]:
    issues: list[tuple[str, str, Any]] = []
    con.row_factory = sqlite3.Row
    for row in con.execute("select id, model_provider, rollout_path, title from threads"):
        path = normalize_windows_path(row["rollout_path"])
        if not path or not path.exists():
            issues.append((row["id"], "missing_jsonl", str(path)))
            continue
        providers = read_jsonl_providers(path)
        if not providers:
            issues.append((row["id"], "missing_session_meta", str(path)))
        elif row["model_provider"] not in providers:
            issues.append(
                (
                    row["id"],
                    "provider_mismatch",
                    {"db": row["model_provider"], "jsonl": sorted(str(v) for v in providers)},
                )
            )
    return issues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-home", default=str(pathlib.Path.home() / ".codex"))
    parser.add_argument("--from-provider", required=True)
    parser.add_argument("--to-provider", required=True)
    parser.add_argument("--cwd", help="Optional workspace path scope, for example D:\\LLM-Wiki")
    parser.add_argument("--apply", action="store_true", help="Write changes. Omit for dry-run.")
    args = parser.parse_args()

    codex_home = pathlib.Path(args.codex_home).expanduser()
    state_db = codex_home / "state_5.sqlite"
    if not state_db.exists():
        print(f"state DB not found: {state_db}", file=sys.stderr)
        return 2

    scope_cwd = None
    if args.cwd:
        scope_cwd_path = normalize_windows_path(args.cwd)
        scope_cwd = str(scope_cwd_path) if scope_cwd_path else args.cwd

    con = connect_db(state_db, readonly=not args.apply)
    rows = load_threads(con, args.from_provider, scope_cwd)
    print(f"matched threads: {len(rows)}")
    if not rows:
        return 0

    missing = []
    for row in rows:
        rollout = normalize_windows_path(row["rollout_path"])
        if not rollout or not rollout.exists():
            missing.append(row["rollout_path"])
    if missing:
        print("missing rollout files:", file=sys.stderr)
        for item in missing[:20]:
            print(f"  {item}", file=sys.stderr)
        return 3

    if not args.apply:
        print("dry-run only. Add --apply to write changes.")
        for row in rows[:20]:
            title = row["title"].replace("\n", " / ")[:90]
            print(f"{row['id'][:8]} {row['model_provider']} {row['cwd']} {title}")
        return 0

    backup_dir = backup_inputs(codex_home, state_db, rows, f"{args.from_provider}-to-{args.to_provider}")
    seen: set[pathlib.Path] = set()
    json_files_changed = 0
    json_meta_changed = 0
    for row in rows:
        path = normalize_windows_path(row["rollout_path"])
        assert path is not None
        if path in seen:
            continue
        seen.add(path)
        changed = update_jsonl_provider(path, args.from_provider, args.to_provider)
        if changed:
            json_files_changed += 1
            json_meta_changed += changed

    with con:
        cur = con.execute(
            "update threads set model_provider = ? where model_provider = ?"
            + (" and lower(cwd) in (?, ?)" if args.cwd else ""),
            (
                (
                    args.to_provider,
                    args.from_provider,
                    scope_cwd.lower(),
                    ("\\\\?\\" + scope_cwd).lower(),
                )
                if scope_cwd
                else (args.to_provider, args.from_provider)
            ),
        )

    issues = verify(con)
    print(f"backup: {backup_dir}")
    print(f"db_threads_updated: {cur.rowcount}")
    print(f"json_files_changed: {json_files_changed}")
    print(f"json_session_meta_changed: {json_meta_changed}")
    print(f"consistency_issues: {len(issues)}")
    for issue in issues[:20]:
        print(issue)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
