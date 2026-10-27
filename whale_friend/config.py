from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"


def atomic_write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode("utf-8") if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_env(path: Path = ROOT / ".env") -> dict[str, str]:
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    # Only explicit application variables override the file; never expose them to UI/logs.
    for key in ("ds_base_url", "base_url", "api_key", "ds_model", "DS_BASE_URL", "DS_API_KEY", "DS_MODEL"):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


@dataclass
class Settings:
    data_dir: str = str(LOCAL / "data")
    backup_dir: str = ""
    model: str = ""
    character_name: str = "小肥鱼"
    user_name: str = "你"
    context_tokens: int = 8192
    reply_tokens: int = 1024
    timeout_seconds: int = 90
    journal_chunk_chars: int = 6000
    idle_minutes: int = 15
    backup_minutes: int = 5
    backup_keep: int = 20

    def validate(self) -> None:
        if not self.character_name.strip() or not self.user_name.strip():
            raise ValueError("角色名字和用户称呼不能为空")
        if not 4096 <= self.context_tokens <= 1_000_000:
            raise ValueError("上下文容量应在 4096 到 1000000 tokens 之间")
        if not 256 <= self.reply_tokens <= self.context_tokens // 3:
            raise ValueError("回复上限至少为 256，且不能超过上下文容量的三分之一")
        for name in ("idle_minutes", "backup_minutes", "backup_keep"):
            if not 1 <= int(getattr(self, name)) <= 1000:
                raise ValueError("归档间隔、备份间隔和保留数量应在 1 到 1000 之间")
        if not 10 <= self.timeout_seconds <= 600:
            raise ValueError("请求超时应在 10 到 600 秒之间")
        if not 1000 <= self.journal_chunk_chars <= 30000:
            raise ValueError("日志分段大小应在 1000 到 30000 字符之间")
        self.data_dir = str(Path(self.data_dir).expanduser().resolve())
        if self.backup_dir.strip():
            backup = Path(self.backup_dir).expanduser().resolve()
            data = Path(self.data_dir)
            if backup == data or backup.is_relative_to(data) or data.is_relative_to(backup):
                raise ValueError("工作数据目录与备份目录应相互独立，不能互相包含")
            self.backup_dir = str(backup)

    def save(self, path: Path = LOCAL / "settings.json") -> None:
        self.validate()
        atomic_write(path, json.dumps(asdict(self), ensure_ascii=False, indent=2))

    @classmethod
    def load(cls, path: Path = LOCAL / "settings.json") -> Settings:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {field.name for field in fields(cls)}
        settings = cls(**{key: value for key, value in data.items() if key in allowed})
        settings.validate()
        return settings
