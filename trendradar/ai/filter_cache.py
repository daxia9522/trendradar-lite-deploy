"""独立本地分类缓存，不改新闻日库。"""

import json
from pathlib import Path
import sqlite3
import time


class ClassificationCache:
    def __init__(self, path):
        path = str(path)
        if path != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.is_symlink():
                raise ValueError("classification cache must not be a symlink")
            if not target.exists():
                # 创建时即限制权限。
                import os
                try:
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    # 兼容并发创建。
                    if target.is_symlink():
                        raise ValueError("classification cache must not be a symlink") from None
                else:
                    os.close(fd)
        self.connection = sqlite3.connect(path, timeout=15)
        try:
            with self.connection:
                self.connection.execute("""CREATE TABLE IF NOT EXISTS ai_filter_cache_v1 (
                    namespace TEXT NOT NULL, title_key TEXT NOT NULL,
                    decision TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(namespace, title_key)
                )""")
                self.connection.execute("DELETE FROM ai_filter_cache_v1 WHERE created_at < ?",
                                        (int(time.time()) - 30 * 86400,))
        except Exception:
            self.connection.close()
            raise

    def get(self, namespace, title_key):
        row = self.connection.execute(
            "SELECT decision FROM ai_filter_cache_v1 WHERE namespace=? AND title_key=?",
            (namespace, title_key),
        ).fetchone()
        return (False, None) if row is None else (True, json.loads(row[0]))

    def put_batch(self, namespace, records):
        # null表示已确认无匹配；未完成的结果不入缓存。
        now = int(time.time())
        rows = [(namespace, key, json.dumps(value, ensure_ascii=False, allow_nan=False), now)
                for key, value in records.items()]
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO ai_filter_cache_v1 VALUES (?, ?, ?, ?)", rows)
            # 并发时沿用先写入的结果。
            return {key: self.get(namespace, key)[1] for key in records}

    def close(self):
        self.connection.close()
