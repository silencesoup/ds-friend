"""Native UI QA entry point; keeps test conversation separate from personal data."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings

app = Desktop(Settings(data_dir=str(LOCAL / "ui-check" / "data"), backup_dir=str(LOCAL / "ui-check" / "backups")))
app.base_title = "开发验证 · 小肥鱼的海边小屋"
app.title(app.base_title)
app.mainloop()
