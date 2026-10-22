"""Резервна копія бази SQLite (безпечно під час роботи бота — через backup API).

    python scripts/backup_db.py            # копія в папку backups, старші за 14 днів видаляються
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import PROJECT_ROOT, load_config  # noqa: E402

KEEP_DAYS = 14


def main() -> int:
    db = load_config().db_path
    if not db.exists():
        print(f"Бази ще немає: {db}")
        return 0
    dest = PROJECT_ROOT / "backups"
    dest.mkdir(exist_ok=True)
    out = dest / f"{db.stem}_{time.strftime('%Y%m%d_%H%M')}.db"
    src, dst = sqlite3.connect(db), sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    cutoff = time.time() - KEEP_DAYS * 86_400
    for old in dest.glob(f"{db.stem}_*.db"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    print(f"Збережено: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
