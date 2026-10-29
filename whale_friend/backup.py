from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import Settings, atomic_write
from .store import Store, now

FORMAT_VERSION = 1
MAX_RESTORE_BYTES = 512 * 1024 * 1024
REQUIRED_TABLES = {"sessions", "messages", "memories", "summaries", "journals", "journal_sources", "forgotten_sources", "meta"}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性校验失败")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("备份数据版本不兼容")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not REQUIRED_TABLES.issubset(tables):
            raise ValueError("备份缺少必要的数据表")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("备份的数据关联校验失败")


class Backups:
    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings

    def create(self, directory: Path | None = None) -> Path | None:
        if directory is None and not self.settings.backup_dir:
            return None
        destination = Path(directory or self.settings.backup_dir)
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        filename = f"whale-friend-{stamp}-{uuid.uuid4().hex[:6]}.zip"
        # Only snapshot using SQLite's backup API. Never copy a live .db + WAL.
        with tempfile.TemporaryDirectory(prefix="whale-backup-") as temporary:
            temporary = Path(temporary)
            snapshot = temporary / "companion.sqlite3"
            self.store.snapshot(snapshot)
            validate_database(snapshot)
            files: dict[str, bytes] = {"companion.sqlite3": snapshot.read_bytes()}
            with closing(sqlite3.connect(snapshot)) as db:
                db.row_factory = sqlite3.Row
                for table in ("sessions", "memories", "summaries"):
                    files[f"exports/{table}.json"] = json.dumps([dict(row) for row in db.execute(f"SELECT * FROM {table}")], ensure_ascii=False, indent=2).encode("utf-8")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "proactive_messages" in tables:
                    files["exports/proactive_messages.json"] = json.dumps([dict(row) for row in db.execute("SELECT * FROM proactive_messages")], ensure_ascii=False, indent=2).encode("utf-8")
                interaction = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM meta WHERE key IN ('companion_state','interaction_preferences','reflection_through','initiative_plan','proactive_skipped_user_id','proactive_retry_after')")}
                files["exports/interaction_state.json"] = json.dumps(interaction, ensure_ascii=False, indent=2).encode("utf-8")
                messages = [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY id")]
                files["exports/messages.jsonl"] = ("\n".join(json.dumps(row, ensure_ascii=False) for row in messages) + "\n").encode("utf-8")
                journals = [dict(row) for row in db.execute("SELECT * FROM journals ORDER BY day,part")]
                days, months = {}, {}
                for journal in journals:
                    day = journal["day"]
                    days.setdefault(day, []).append(f"## 第 {journal['part']} 段 · {journal['title']}\n\n{journal['content']}\n\n来源消息：{journal['source_ids']}\n")
                    months.setdefault(day[:7], []).append(f"- [{day} 第{journal['part']}段](../{day}.md)：{journal['title']} — {journal['content'].replace(chr(10), ' ')[:180]}")
                for day, entries in days.items():
                    files[f"journals/{day}.md"] = (f"# {day} 对话日志\n\n" + "\n".join(entries)).encode("utf-8")
                for month, entries in months.items():
                    files[f"journals/index/{month}.md"] = (f"# {month} 日志索引\n\n" + "\n".join(entries)).encode("utf-8")
                files["journals/INDEX.md"] = ("# 日志索引\n\n" + "\n".join(f"- [{month}](index/{month}.md)" for month in sorted(months, reverse=True))).encode("utf-8")
            safe_settings = asdict(self.settings)
            safe_settings.pop("data_dir", None)
            safe_settings.pop("backup_dir", None)
            files["settings.json"] = json.dumps(safe_settings, ensure_ascii=False, indent=2).encode("utf-8")
            if sum(len(content) for content in files.values()) > MAX_RESTORE_BYTES - 1024 * 1024:
                raise ValueError("数据超过第一版恢复包容量上限；本地数据仍保留，请升级备份格式后继续")
            manifest = {"format": "whale-friend", "version": FORMAT_VERSION, "created_at": now(), "files": {name: digest(content) for name, content in files.items()}, "counts": {"messages": len(messages), "journals": len(journals)}}
            archive = temporary / filename
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_:
                for name, content in files.items():
                    zip_.writestr(name, content)
                zip_.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            # Atomic rename prevents a partially written file from looking recoverable.
            fd, staging = tempfile.mkstemp(prefix=".whale-writing-", suffix=".tmp", dir=destination)
            try:
                with os.fdopen(fd, "wb") as output, archive.open("rb") as source:
                    shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                final = destination / filename
                os.replace(staging, final)
            finally:
                if os.path.exists(staging):
                    os.unlink(staging)
            atomic_write(destination / "LATEST.json", json.dumps({"file": filename, "sha256": digest(final.read_bytes()), "created_at": manifest["created_at"]}, ensure_ascii=False, indent=2))
        self.store.set_meta("last_backup_at", now())
        self.store.set_meta("last_backup_file", str(final))
        # Retention applies only to this application's precisely named recovery packages.
        archives = sorted(destination.glob("whale-friend-*.zip"), reverse=True)
        for old in archives[self.settings.backup_keep:]:
            old.unlink()
        return final

    @staticmethod
    def inspect(archive: Path) -> tuple[dict, bytes]:
        try:
            with zipfile.ZipFile(archive) as zip_:
                infos = zip_.infolist()
                if len(infos) > 20000 or sum(item.file_size for item in infos) > MAX_RESTORE_BYTES:
                    raise ValueError("恢复包过大，超过第一版支持的 512 MB 解压上限")
                names = [item.filename for item in infos]
                if len(set(names)) != len(names):
                    raise ValueError("恢复包有重复条目")
                if "manifest.json" not in names or "companion.sqlite3" not in names:
                    raise ValueError("不是完整的鲸鱼娘恢复包")
                manifest = json.loads(zip_.read("manifest.json"))
                if not isinstance(manifest, dict) or manifest.get("format") != "whale-friend" or manifest.get("version") != FORMAT_VERSION:
                    raise ValueError("恢复包版本不受支持")
                hashes = manifest.get("files", {})
                if not isinstance(hashes, dict) or set(hashes) != set(names) - {"manifest.json"}:
                    raise ValueError("恢复包文件清单不完整")
                for name, expected in hashes.items():
                    # Read entries by name; never extract archive paths to disk.
                    if digest(zip_.read(name)) != expected:
                        raise ValueError("恢复包校验失败，文件可能损坏或尚未同步完成")
                database = zip_.read("companion.sqlite3")
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, TypeError):
            raise ValueError("恢复包无法读取，可能损坏或尚未同步完成") from None
        with tempfile.TemporaryDirectory(prefix="whale-verify-") as temporary:
            candidate = Path(temporary) / "verify.sqlite3"
            candidate.write_bytes(database)
            try:
                validate_database(candidate)
            except sqlite3.DatabaseError:
                raise ValueError("恢复包中的数据库损坏，无法恢复") from None
        return manifest, database

    @classmethod
    def restore(cls, archive: Path, destination: Path) -> dict:
        manifest, database = cls.inspect(archive)
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise ValueError("请选择一个空的恢复目录，现有数据不会被覆盖")
        atomic_write(destination / "companion.sqlite3", database)
        return manifest
