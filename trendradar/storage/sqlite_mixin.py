# coding=utf-8
"""
SQLite 存储 Mixin

提供共用的 SQLite 数据库操作逻辑，供 LocalStorageBackend 和 RemoteStorageBackend 复用。
"""

import sqlite3
from abc import abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from trendradar.storage.base import NewsData, RSSData
from trendradar.utils.url import normalize_url


# Keep all storage SQLite connections on the same bounded wait policy.  The
# timeout is deliberately finite: a stuck writer must eventually surface an
# error to the caller instead of hanging a scheduled run forever.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def connect_sqlite(
    path: Path | str,
    *,
    readonly: bool = False,
    wal: bool = False,
    immutable: bool = False,
) -> sqlite3.Connection:
    """Open a storage SQLite connection with explicit locking semantics.

    ``wal`` is only used by the local writer.  Remote databases are uploaded
    as a single main file, so they intentionally stay on the rollback journal
    and never depend on an un-uploaded ``-wal`` sidecar.  Read-only callers
    use SQLite's URI ``mode=ro`` and therefore cannot create or mutate a DB.
    """
    if immutable and not readonly:
        raise ValueError("Immutable SQLite connections must be read-only")
    db_path = Path(path).resolve()
    if readonly:
        connect_target = f"{db_path.as_uri()}?mode=ro"
        if immutable:
            connect_target += "&immutable=1"
        conn = sqlite3.connect(
            connect_target,
            uri=True,
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )
    else:
        conn = sqlite3.connect(
            str(db_path),
            timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        )

    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        if readonly:
            conn.execute("PRAGMA query_only = ON")
        else:
            journal_mode = "WAL" if wal else "DELETE"
            actual_mode = conn.execute(f"PRAGMA journal_mode = {journal_mode}").fetchone()[0]
            if str(actual_mode).lower() != journal_mode.lower():
                raise sqlite3.OperationalError(
                    f"SQLite journal mode {actual_mode!r} != {journal_mode!r}"
                )
        return conn
    except BaseException:
        conn.close()
        raise


class SQLiteStorageMixin:
    """
    SQLite 存储操作 Mixin

    子类需要实现以下抽象方法：
    - _get_connection(date, db_type) -> sqlite3.Connection
    - _get_configured_time() -> datetime
    - _format_date_folder(date) -> str
    """

    # ========================================
    # 抽象方法 - 子类必须实现
    # ========================================

    @abstractmethod
    def _get_connection(self, date: Optional[str] = None, db_type: str = "news") -> sqlite3.Connection:
        """获取数据库连接"""
        pass


    @abstractmethod
    def _get_configured_time(self) -> datetime:
        """获取配置时区的当前时间"""
        pass

    @abstractmethod
    def _format_date_folder(self, date: Optional[str] = None) -> str:
        """格式化日期文件夹名 (ISO 格式: YYYY-MM-DD)"""
        pass


    # ========================================
    # Schema 管理
    # ========================================

    def _get_schema_path(self, db_type: str = "news") -> Path:
        """
        获取 schema.sql 文件路径

        Args:
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            schema 文件路径
        """
        if db_type == "rss":
            return Path(__file__).parent / "rss_schema.sql"
        return Path(__file__).parent / "schema.sql"


    def _init_tables(self, conn: sqlite3.Connection, db_type: str = "news") -> None:
        """
        从 schema.sql 初始化数据库表结构

        Args:
            conn: 数据库连接
            db_type: 数据库类型 ("news" 或 "rss")
        """
        schema_sql = self._get_schema_path(db_type).read_text(encoding="utf-8")
        # Acquire the write lock before inspecting/migrating the schema so two
        # starters cannot race to add the same column. Never commit caller data.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if db_type == "rss":
                table_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rss_items'"
                ).fetchone()
                if table_exists:
                    self._migrate_rss_schema(conn)

            # executescript() implicitly commits a pending transaction. Use
            # SQLite's own statement-completeness check, including triggers and
            # quoted semicolons, to execute this schema within our transaction.
            statement = []
            for character in schema_sql:
                statement.append(character)
                if character == ";" and sqlite3.complete_statement("".join(statement)):
                    conn.execute("".join(statement))
                    statement.clear()
            if "".join(statement).strip():
                conn.execute("".join(statement))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _migrate_rss_schema(self, conn: sqlite3.Connection) -> None:
        """为已有 RSS 数据库补充 GUID 字段和唯一索引。"""
        cursor = conn.execute("PRAGMA table_info(rss_items)")
        columns = {row[1] for row in cursor.fetchall()}
        if "guid" not in columns:
            conn.execute("ALTER TABLE rss_items ADD COLUMN guid TEXT DEFAULT ''")

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rss_guid_feed
            ON rss_items(guid, feed_id) WHERE guid != ''
        """)

    # ========================================
    # 新闻数据存储
    # ========================================

    def _save_news_data_impl(self, data: NewsData, log_prefix: str = "[存储]") -> tuple[bool, int, int, int, int]:
        """
        保存新闻数据到 SQLite（核心实现）

        Args:
            data: 新闻数据
            log_prefix: 日志前缀

        Returns:
            (success, new_count, updated_count, title_changed_count, off_list_count)
        """
        try:
            conn = self._get_connection(data.date)
            cursor = conn.cursor()

            # 获取配置时区的当前时间
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            # 首先同步平台信息到 platforms 表
            for source_id, source_name in data.id_to_name.items():
                cursor.execute("""
                    INSERT INTO platforms (id, name, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        updated_at = excluded.updated_at
                """, (source_id, source_name, now_str))

            # 统计计数器
            new_count = 0
            updated_count = 0
            title_changed_count = 0
            success_sources = []

            for source_id, news_list in data.items.items():
                success_sources.append(source_id)

                for item in news_list:
                    try:
                        # 标准化 URL（去除动态参数，如微博的 band_rank）
                        normalized_url = normalize_url(item.url, source_id) if item.url else ""

                        # 检查是否已存在（通过标准化 URL + platform_id）
                        if normalized_url:
                            cursor.execute("""
                                SELECT id, title FROM news_items
                                WHERE url = ? AND platform_id = ?
                            """, (normalized_url, source_id))
                            existing = cursor.fetchone()

                            if existing:
                                # 已存在，更新记录
                                existing_id, existing_title = existing

                                # 检查标题是否变化
                                if existing_title != item.title:
                                    # 记录标题变更
                                    cursor.execute("""
                                        INSERT INTO title_changes
                                        (news_item_id, old_title, new_title, changed_at)
                                        VALUES (?, ?, ?, ?)
                                    """, (existing_id, existing_title, item.title, now_str))
                                    title_changed_count += 1

                                # 记录排名历史
                                cursor.execute("""
                                    INSERT INTO rank_history
                                    (news_item_id, rank, crawl_time, created_at)
                                    VALUES (?, ?, ?, ?)
                                """, (existing_id, item.rank, data.crawl_time, now_str))

                                # 更新现有记录
                                cursor.execute("""
                                    UPDATE news_items SET
                                        title = ?,
                                        rank = ?,
                                        mobile_url = ?,
                                        last_crawl_time = ?,
                                        crawl_count = crawl_count + 1,
                                        updated_at = ?
                                    WHERE id = ?
                                """, (item.title, item.rank, item.mobile_url,
                                      data.crawl_time, now_str, existing_id))
                                updated_count += 1
                            else:
                                # 不存在，插入新记录（存储标准化后的 URL）
                                cursor.execute("""
                                    INSERT INTO news_items
                                    (title, platform_id, rank, url, mobile_url,
                                     first_crawl_time, last_crawl_time, crawl_count,
                                     created_at, updated_at)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                                """, (item.title, source_id, item.rank, normalized_url,
                                      item.mobile_url, data.crawl_time, data.crawl_time,
                                      now_str, now_str))
                                new_id = cursor.lastrowid
                                # 记录初始排名
                                cursor.execute("""
                                    INSERT INTO rank_history
                                    (news_item_id, rank, crawl_time, created_at)
                                    VALUES (?, ?, ?, ?)
                                """, (new_id, item.rank, data.crawl_time, now_str))
                                new_count += 1
                        else:
                            # URL 为空的情况，直接插入（不做去重）
                            cursor.execute("""
                                INSERT INTO news_items
                                (title, platform_id, rank, url, mobile_url,
                                 first_crawl_time, last_crawl_time, crawl_count,
                                 created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """, (item.title, source_id, item.rank, "",
                                  item.mobile_url, data.crawl_time, data.crawl_time,
                                  now_str, now_str))
                            new_id = cursor.lastrowid
                            # 记录初始排名
                            cursor.execute("""
                                INSERT INTO rank_history
                                (news_item_id, rank, crawl_time, created_at)
                                VALUES (?, ?, ?, ?)
                            """, (new_id, item.rank, data.crawl_time, now_str))
                            new_count += 1

                    except sqlite3.Error as e:
                        print(f"{log_prefix} 保存新闻条目失败 [{item.title[:30]}...]: {e}")

            total_items = new_count + updated_count

            # ========================================
            # 脱榜检测：检测上次在榜但这次不在榜的新闻
            # ========================================
            off_list_count = 0

            # 获取上一次抓取时间
            cursor.execute("""
                SELECT crawl_time FROM crawl_records
                WHERE crawl_time < ?
                ORDER BY crawl_time DESC
                LIMIT 1
            """, (data.crawl_time,))
            prev_record = cursor.fetchone()

            if prev_record:
                prev_crawl_time = prev_record[0]

                # 对于每个成功抓取的平台，检测脱榜
                for source_id in success_sources:
                    # 获取当前抓取中该平台的所有标准化 URL
                    current_urls = set()
                    for item in data.items.get(source_id, []):
                        normalized_url = normalize_url(item.url, source_id) if item.url else ""
                        if normalized_url:
                            current_urls.add(normalized_url)

                    # 查询上次在榜（last_crawl_time = prev_crawl_time）但这次不在榜的新闻
                    # 这些新闻是"第一次脱榜"，需要记录
                    cursor.execute("""
                        SELECT id, url FROM news_items
                        WHERE platform_id = ?
                          AND last_crawl_time = ?
                          AND url != ''
                    """, (source_id, prev_crawl_time))

                    for row in cursor.fetchall():
                        news_id, url = row[0], row[1]
                        if url not in current_urls:
                            # 插入脱榜记录（rank=0 表示脱榜）
                            cursor.execute("""
                                INSERT INTO rank_history
                                (news_item_id, rank, crawl_time, created_at)
                                VALUES (?, 0, ?, ?)
                            """, (news_id, data.crawl_time, now_str))
                            off_list_count += 1

            # 记录抓取信息
            cursor.execute("""
                INSERT OR REPLACE INTO crawl_records
                (crawl_time, total_items, created_at)
                VALUES (?, ?, ?)
            """, (data.crawl_time, total_items, now_str))

            # 获取刚插入的 crawl_record 的 ID
            cursor.execute("""
                SELECT id FROM crawl_records WHERE crawl_time = ?
            """, (data.crawl_time,))
            record_row = cursor.fetchone()
            if record_row:
                crawl_record_id = record_row[0]

                # 记录成功的来源
                for source_id in success_sources:
                    cursor.execute("""
                        INSERT OR REPLACE INTO crawl_source_status
                        (crawl_record_id, platform_id, status)
                        VALUES (?, ?, 'success')
                    """, (crawl_record_id, source_id))

                # 记录失败的来源
                for failed_id in data.failed_ids:
                    # 确保失败的平台也在 platforms 表中
                    cursor.execute("""
                        INSERT OR IGNORE INTO platforms (id, name, updated_at)
                        VALUES (?, ?, ?)
                    """, (failed_id, failed_id, now_str))

                    cursor.execute("""
                        INSERT OR REPLACE INTO crawl_source_status
                        (crawl_record_id, platform_id, status)
                        VALUES (?, ?, 'failed')
                    """, (crawl_record_id, failed_id))

            conn.commit()

            return True, new_count, updated_count, title_changed_count, off_list_count

        except Exception as e:
            # Preserve transaction atomicity when a non-item-level failure
            # aborts the save (item-level errors are intentionally isolated
            # above for compatibility with existing ingestion behavior).
            try:
                conn.rollback()
            except (NameError, sqlite3.Error):
                pass
            print(f"{log_prefix} 保存失败: {e}")
            return False, 0, 0, 0, 0


    # ========================================
    # 时间段执行记录（调度系统）
    # ========================================

    def _has_period_executed_impl(self, date_str: str, period_key: str, action: str) -> bool:
        """
        检查指定时间段的某个 action 今天是否已执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否已执行
        """
        try:
            conn = self._get_connection(date_str)
            cursor = conn.cursor()

            # 先检查表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='period_executions'
            """)
            if not cursor.fetchone():
                return False

            cursor.execute("""
                SELECT 1 FROM period_executions
                WHERE execution_date = ? AND period_key = ? AND action = ?
            """, (date_str, period_key, action))

            return cursor.fetchone() is not None

        except Exception as e:
            print(f"[存储] 检查时间段执行记录失败: {e}")
            return False

    def _record_period_execution_impl(self, date_str: str, period_key: str, action: str) -> bool:
        """
        记录时间段的 action 执行

        Args:
            date_str: 日期字符串 YYYY-MM-DD
            period_key: 时间段 key
            action: 动作类型 (analyze / push)

        Returns:
            是否记录成功
        """
        try:
            conn = self._get_connection(date_str)
            cursor = conn.cursor()

            # 确保表存在
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS period_executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_date TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(execution_date, period_key, action)
                )
            """)

            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("""
                INSERT OR IGNORE INTO period_executions (execution_date, period_key, action, executed_at)
                VALUES (?, ?, ?, ?)
            """, (date_str, period_key, action, now_str))

            conn.commit()
            return True

        except Exception as e:
            try:
                conn.rollback()
            except (NameError, sqlite3.Error):
                pass
            print(f"[存储] 记录时间段执行失败: {e}")
            return False

    # ========================================
    # RSS 数据存储
    # ========================================

    def _save_rss_data_impl(self, data: RSSData, log_prefix: str = "[存储]") -> tuple[bool, int, int]:
        """
        保存 RSS 数据到 SQLite（以 URL 为唯一标识）

        Args:
            data: RSS 数据
            log_prefix: 日志前缀

        Returns:
            (success, new_count, updated_count)
        """
        try:
            conn = self._get_connection(data.date, db_type="rss")
            cursor = conn.cursor()

            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")

            # 同步 RSS 源信息到 rss_feeds 表
            for feed_id, feed_name in data.id_to_name.items():
                cursor.execute("""
                    INSERT INTO rss_feeds (id, name, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        updated_at = excluded.updated_at
                """, (feed_id, feed_name, now_str))

            # 统计计数器
            new_count = 0
            updated_count = 0
            feed_errors: Dict[str, List[str]] = {}

            for feed_id, rss_list in data.items.items():
                for item in rss_list:
                    try:
                        item_guid = str(getattr(item, "guid", "") or "").strip()
                        item_url = str(item.url or "").strip()
                        if not item_url and not item_guid:
                            raise sqlite3.Error("缺少 URL 和 GUID")

                        guid_row = None
                        url_row = None

                        # 优先按稳定 GUID 定位，URL 作为 GUID 缺失时的回退身份
                        if item_guid:
                            cursor.execute("""
                                SELECT id, title, url, guid, first_crawl_time, last_crawl_time, crawl_count
                                FROM rss_items
                                WHERE guid = ? AND feed_id = ?
                            """, (item_guid, feed_id))
                            guid_row = cursor.fetchone()

                        if item_url:
                            cursor.execute("""
                                SELECT id, title, url, guid, first_crawl_time, last_crawl_time, crawl_count
                                FROM rss_items
                                WHERE url = ? AND feed_id = ?
                            """, (item_url, feed_id))
                            url_row = cursor.fetchone()

                        existing = guid_row or url_row
                        if existing:
                            existing_id = existing[0]
                            crawl_count = existing[6] + 1
                            first_crawl_time = existing[4] or data.crawl_time

                            # GUID 与 URL 分别命中两条记录：保留 GUID 行，合并 URL 行
                            if guid_row and url_row and url_row[0] != guid_row[0]:
                                crawl_count = existing[6] + url_row[6] + 1
                                first_crawl_time = min(
                                    existing[4] or data.crawl_time,
                                    url_row[4] or data.crawl_time,
                                )
                                cursor.execute(
                                    "DELETE FROM rss_items WHERE id = ?",
                                    (url_row[0],),
                                )

                            cursor.execute("""
                                UPDATE rss_items SET
                                    title = ?,
                                    url = CASE WHEN ? != '' THEN ? ELSE url END,
                                    guid = CASE WHEN ? != '' THEN ? ELSE guid END,
                                    published_at = ?,
                                    summary = ?,
                                    author = ?,
                                    first_crawl_time = ?,
                                    last_crawl_time = ?,
                                    crawl_count = ?,
                                    updated_at = ?
                                WHERE id = ?
                            """, (
                                item.title,
                                item_url, item_url,
                                item_guid, item_guid,
                                item.published_at,
                                item.summary,
                                item.author,
                                first_crawl_time,
                                data.crawl_time,
                                crawl_count,
                                now_str,
                                existing_id,
                            ))
                            updated_count += 1
                        else:
                            cursor.execute("""
                                INSERT INTO rss_items
                                (title, feed_id, url, guid, published_at, summary, author,
                                 first_crawl_time, last_crawl_time, crawl_count,
                                 created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                            """, (
                                item.title,
                                feed_id,
                                item_url,
                                item_guid,
                                item.published_at,
                                item.summary,
                                item.author,
                                data.crawl_time,
                                data.crawl_time,
                                now_str,
                                now_str,
                            ))
                            new_count += 1

                    except sqlite3.Error as e:
                        error_text = f"{item.title[:30]}...: {e}"
                        feed_errors.setdefault(feed_id, []).append(error_text)
                        print(f"{log_prefix} 保存 RSS 条目失败 [{error_text}]")

            total_items = new_count + updated_count

            # 记录抓取信息
            cursor.execute("""
                INSERT OR REPLACE INTO rss_crawl_records
                (crawl_time, total_items, created_at)
                VALUES (?, ?, ?)
            """, (data.crawl_time, total_items, now_str))

            # 记录抓取状态
            cursor.execute("""
                SELECT id FROM rss_crawl_records WHERE crawl_time = ?
            """, (data.crawl_time,))
            record_row = cursor.fetchone()
            if record_row:
                crawl_record_id = record_row[0]

                # 记录成功的源（存在条目级失败的源不标记 success）
                for feed_id in data.items.keys():
                    if feed_id in feed_errors:
                        continue
                    cursor.execute("""
                        INSERT OR REPLACE INTO rss_crawl_status
                        (crawl_record_id, feed_id, status)
                        VALUES (?, ?, 'success')
                    """, (crawl_record_id, feed_id))

                # 记录抓取级失败的源
                for failed_id in data.failed_ids:
                    cursor.execute("""
                        INSERT OR IGNORE INTO rss_feeds (id, name, updated_at)
                        VALUES (?, ?, ?)
                    """, (failed_id, failed_id, now_str))

                    cursor.execute("""
                        INSERT OR REPLACE INTO rss_crawl_status
                        (crawl_record_id, feed_id, status)
                        VALUES (?, ?, 'failed')
                    """, (crawl_record_id, failed_id))

                # 记录条目级失败的源
                for failed_feed_id, errors in feed_errors.items():
                    error_message = f"{len(errors)} 条 RSS 条目保存失败: " + "; ".join(errors[:3])
                    cursor.execute("""
                        INSERT OR IGNORE INTO rss_feeds (id, name, updated_at)
                        VALUES (?, ?, ?)
                    """, (failed_feed_id, failed_feed_id, now_str))

                    cursor.execute("""
                        INSERT OR REPLACE INTO rss_crawl_status
                        (crawl_record_id, feed_id, status, error_message)
                        VALUES (?, ?, 'failed', ?)
                    """, (crawl_record_id, failed_feed_id, error_message))

            conn.commit()

            return True, new_count, updated_count

        except Exception as e:
            try:
                conn.rollback()
            except (NameError, sqlite3.Error):
                pass
            print(f"{log_prefix} 保存 RSS 数据失败: {e}")
            return False, 0, 0
