from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

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
                if value[0] == '"':
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = value[1:-1]
                else:
                    value = value[1:-1]
            result[key.strip()] = value
    # Only explicit application variables override the file; never log credentials.
    for key in ("ds_base_url", "base_url", "api_key", "ds_model", "DS_BASE_URL", "DS_API_KEY", "DS_MODEL"):
        if key in os.environ:
            result[key] = os.environ[key]
    return result


@dataclass
class ConnectionConfig:
    """Connection form values live in .env, never in settings or memory backups."""
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""

    @classmethod
    def load(cls, env: dict | None = None, legacy_model: str = "") -> ConnectionConfig:
        env = read_env() if env is None else env
        return cls(env.get("DS_BASE_URL") or env.get("ds_base_url") or env.get("base_url") or "",
                   env.get("DS_API_KEY") or env.get("api_key") or "",
                   legacy_model or env.get("DS_MODEL") or env.get("ds_model") or "")

    def validate(self) -> None:
        self.base_url, self.api_key, self.model = self.base_url.strip(), self.api_key.strip(), self.model.strip()
        if any(ord(char) < 32 or ord(char) == 127 for value in (self.base_url, self.api_key, self.model) for char in value):
            raise ValueError("API 地址、密钥和模型名称不能包含换行或控制字符")
        try:
            parsed = urlparse(self.base_url)
            valid = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
            parsed.port  # Validate malformed and out-of-range ports without exposing the URL.
        except ValueError:
            valid = False
        if not valid or any(char.isspace() for char in self.base_url) or parsed.query or parsed.fragment:
            raise ValueError("请在设置中填写有效的 API 地址（http:// 或 https://，不要包含登录凭据、查询参数或片段）")

    def as_env(self) -> dict[str, str]:
        return {"ds_base_url": self.base_url, "api_key": self.api_key, "ds_model": self.model}

    def save(self, path: Path = ROOT / ".env") -> None:
        self.validate()
        # Update existing aliases as well, so a stale uppercase/file alias cannot win.
        groups = {"ds_base_url": ("ds_base_url", "base_url", "DS_BASE_URL"),
                  "api_key": ("api_key", "DS_API_KEY"), "ds_model": ("ds_model", "DS_MODEL")}
        values = {alias: self.as_env()[canonical] for canonical, aliases in groups.items() for alias in aliases}
        # Explicit process environment values retain their documented precedence.
        conflicts = [key for key, value in values.items() if key in os.environ and os.environ[key] != value]
        if conflicts:
            raise ValueError("启动环境变量优先于 .env，请先修改或移除这些变量，再保存：" + "、".join(conflicts))
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
        found = set()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key = stripped.removeprefix("export ").split("=", 1)[0].strip()
            if key in values:
                lines[index] = key + "=" + json.dumps(values[key], ensure_ascii=False)
                found.add(key)
        for canonical, aliases in groups.items():
            if not found.intersection(aliases):
                lines.append(canonical + "=" + json.dumps(values[canonical], ensure_ascii=False))
        atomic_write(path, "\n".join(lines) + "\n")


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
    proactive_enabled: bool = True
    proactive_min_minutes: int = 15
    proactive_max_minutes: int = 120
    proactive_cooldown_minutes: int = 120
    proactive_daily_limit: int = 3
    quiet_start_hour: int = 22
    quiet_end_hour: int = 8
    adaptive_enabled: bool = True
    mobile_enabled: bool = False
    mobile_port: int = 8765
    memory_tools_enabled: bool = True
    memory_consolidation_enabled: bool = True
    memory_evolution_enabled: bool = True
    vision_mode: str = "auto"
    pet_enabled: bool = False
    pet_plugin_dir: str = ""
    pet_electron_path: str = ""
    pet_size: int = 462
    pet_fixed: bool = False
    pet_voice_enabled: bool = False
    pet_voice_name: str = "zh-CN-XiaoyiNeural"
    animation_enabled: bool = True

    def validate(self) -> None:
        if type(self.memory_evolution_enabled) is not bool:
            raise ValueError('明确状态变化自动更新开关必须为布尔值')
        if type(self.animation_enabled) is not bool:
            raise ValueError("角色动画开关必须为布尔值")
        if type(self.pet_voice_enabled) is not bool or self.pet_voice_name not in ('zh-CN-XiaoyiNeural','zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural','zh-CN-YunjianNeural'):
            raise ValueError("桌宠朗读开关或音色不正确")
        if type(self.pet_enabled) is not bool or type(self.pet_fixed) is not bool:
            raise ValueError("桌宠开关必须为布尔值")
        if type(self.pet_size) is not int or not 240 <= self.pet_size <= 900:
            raise ValueError("桌宠尺寸应为 240–900")
        if not isinstance(self.pet_plugin_dir, str) or not isinstance(self.pet_electron_path, str):
            raise ValueError("桌宠路径格式不正确")
        if self.vision_mode not in ("auto", "on", "off"):
            raise ValueError("识图模式应为 auto、on 或 off")
        if type(self.memory_tools_enabled) is not bool:
            raise ValueError("对话记忆工具开关必须为布尔值")
        if type(self.memory_consolidation_enabled) is not bool:
            raise ValueError("后台记忆归纳开关必须为布尔值")
        if type(self.mobile_enabled) is not bool or type(self.mobile_port) is not int or not 1024 <= self.mobile_port <= 65535:
            raise ValueError("手机连接开关须为布尔值，端口应在 1024 到 65535 之间")
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
        if type(self.proactive_enabled) is not bool or type(self.adaptive_enabled) is not bool:
            raise ValueError("主动聊天与互动学习的开关必须为布尔值")
        if not 1 <= self.proactive_min_minutes <= self.proactive_max_minutes <= 1440:
            raise ValueError("主动聊天浮动范围应在 1–1440 分钟之间，最短等待不能大于最长等待")
        if not 1 <= self.proactive_cooldown_minutes <= 10080:
            raise ValueError("主动聊天冷却时间应为 1–10080 分钟")
        if not 1 <= self.proactive_daily_limit <= 20:
            raise ValueError("主动聊天每日上限应为 1–20 次")
        if not 0 <= self.quiet_start_hour <= 23 or not 0 <= self.quiet_end_hour <= 23:
            raise ValueError("免打扰时段请填写 0–23 点；起止时间相同表示不设时段")
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
