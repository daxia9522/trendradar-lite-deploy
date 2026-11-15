# coding=utf-8
"""
本地存储后端 - SQLite + TXT

使用 SQLite 作为主存储，支持可选的 TXT 快照
"""

import sqlite3
import shutil
import pytz
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from trendradar.storage.base import StorageBackend, NewsData, RSSData
from trendradar.storage.sqlite_mixin import SQLiteStorageMixin, connect_sqlite
from trendradar.utils.time import (
    DEFAULT_TIMEZONE,
    get_configured_time,
    format_date_folder,
)


class LocalStorageBackend(SQLiteStorageMixin, StorageBackend):
    """
    本地存储后端

    使用 SQLite 数据库存储新闻数据，支持：
    - 按日期组织的 SQLite 数据库文件
    - 可选的 TXT 快照（用于调试）
    """

    def __init__(
        self,
        data_dir: str = "output",
        enable_txt: bool = True,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        """
        初始化本地存储后端

        Args:
            data_dir: 数据目录路径
            enable_txt: 是否启用 TXT 快照
            timezone: 时区配置
        """
        self.data_dir = Path(data_dir)
        self.enable_txt = enable_txt
        self.timezone = timezone
        self._db_connections: Dict[str, sqlite3.Connection] = {}

    @property
    def backend_name(self) -> str:
        return "local"

    @property
    def supports_txt(self) -> bool:
        return self.enable_txt

    # ========================================
    # SQLiteStorageMixin 抽象方法实现
    # ========================================

    def _get_configured_time(self) -> datetime:
        """获取配置时区的当前时间"""
        return get_configured_time(self.timezone)

    def _format_date_folder(self, date: Optional[str] = None) -> str:
        """格式化日期文件夹名 (ISO 格式: YYYY-MM-DD)"""
        return format_date_folder(date, self.timezone)


    def _get_db_path(self, date: Optional[str] = None, db_type: str = "news") -> Path:
        """
        获取 SQLite 数据库路径

        新结构（扁平）：output/{type}/{date}.db
        - output/news/2025-12-28.db
        - output/rss/2025-12-28.db

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            数据库文件路径
        """
        date_str = self._format_date_folder(date)
        db_dir = self.data_dir / db_type
        db_dir.mkdir(parents=True, exist_ok=True)
        return db_dir / f"{date_str}.db"

    def _get_connection(self, date: Optional[str] = None, db_type: str = "news") -> sqlite3.Connection:
        """
        获取数据库连接（带缓存）

        Args:
            date: 日期字符串
            db_type: 数据库类型 ("news" 或 "rss")

        Returns:
            数据库连接
        """
        db_path = str(self._get_db_path(date, db_type))

        if db_path not in self._db_connections:
            conn = connect_sqlite(db_path, wal=True)
            try:
                self._init_tables(conn, db_type)
            except Exception:
                conn.close()
                raise
            self._db_connections[db_path] = conn

        return self._db_connections[db_path]

    # ========================================
    # StorageBackend 接口实现（委托给 mixin）
    # ========================================

    def save_news_data(self, data: NewsData) -> bool:
        """保存新闻数据到 SQLite"""
        db_path = self._get_db_path(data.date)
        if not db_path.exists():
            # 确保目录存在
            db_path.parent.mkdir(parents=True, exist_ok=True)

        success, new_count, updated_count, title_changed_count, off_list_count = \
            self._save_news_data_impl(data, "[本地存储]")

        if success:
            # 输出详细的存储统计日志
            log_parts = [f"[本地存储] 处理完成：新增 {new_count} 条"]
            if updated_count > 0:
                log_parts.append(f"更新 {updated_count} 条")
            if title_changed_count > 0:
                log_parts.append(f"标题变更 {title_changed_count} 条")
            if off_list_count > 0:
                log_parts.append(f"脱榜 {off_list_count} 条")
            print("，".join(log_parts))

        return success


    # ========================================
    # 时间段执行记录（调度系统）
    # ========================================

    def has_period_executed(self, date_str: str, period_key: str, action: str) -> bool:
        """检查指定时间段的某个 action 是否已执行"""
        return self._has_period_executed_impl(date_str, period_key, action)

    def record_period_execution(self, date_str: str, period_key: str, action: str) -> bool:
        """记录时间段的 action 执行"""
        success = self._record_period_execution_impl(date_str, period_key, action)
        if success:
            now_str = self._get_configured_time().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[本地存储] 时间段执行记录已保存: {period_key}/{action} at {now_str}")
        return success

    # ========================================
    # RSS 数据存储方法
    # ========================================

    def save_rss_data(self, data: RSSData) -> bool:
        """保存 RSS 数据到 SQLite"""
        success, new_count, updated_count = self._save_rss_data_impl(data, "[本地存储]")

        if success:
            # 输出统计日志
            log_parts = [f"[本地存储] RSS 处理完成：新增 {new_count} 条"]
            if updated_count > 0:
                log_parts.append(f"更新 {updated_count} 条")
            print("，".join(log_parts))

        return success


    # ========================================
    # 本地特有功能：TXT 快照
    # ========================================

    def save_txt_snapshot(self, data: NewsData) -> Optional[str]:
        """
        保存 TXT 快照

        新结构：output/txt/{date}/{time}.txt

        Args:
            data: 新闻数据

        Returns:
            保存的文件路径
        """
        if not self.enable_txt:
            return None

        try:
            date_folder = self._format_date_folder(data.date)
            txt_dir = self.data_dir / "txt" / date_folder
            txt_dir.mkdir(parents=True, exist_ok=True)

            file_path = txt_dir / f"{data.crawl_time}.txt"

            with open(file_path, "w", encoding="utf-8") as f:
                for source_id, news_list in data.items.items():
                    source_name = data.id_to_name.get(source_id, source_id)

                    # 写入来源标题
                    if source_name and source_name != source_id:
                        f.write(f"{source_id} | {source_name}\n")
                    else:
                        f.write(f"{source_id}\n")

                    # 按排名排序
                    sorted_news = sorted(news_list, key=lambda x: x.rank)

                    for item in sorted_news:
                        line = f"{item.rank}. {item.title}"
                        if item.url:
                            line += f" [URL:{item.url}]"
                        if item.mobile_url:
                            line += f" [MOBILE:{item.mobile_url}]"
                        f.write(line + "\n")

                    f.write("\n")

                # 写入失败的来源
                if data.failed_ids:
                    f.write("==== 以下ID请求失败 ====\n")
                    for failed_id in data.failed_ids:
                        f.write(f"{failed_id}\n")

            print(f"[本地存储] TXT 快照已保存: {file_path}")
            return str(file_path)

        except Exception as e:
            print(f"[本地存储] 保存 TXT 快照失败: {e}")
            return None


    # ========================================
    # 本地特有功能：资源清理
    # ========================================

    def cleanup(self) -> None:
        """清理资源（关闭数据库连接）"""
        for db_path, conn in self._db_connections.items():
            try:
                conn.close()
                print(f"[本地存储] 关闭数据库连接: {db_path}")
            except Exception as e:
                print(f"[本地存储] 关闭连接失败 {db_path}: {e}")

        self._db_connections.clear()

    def cleanup_old_data(self, retention_days: int) -> int:
        """
        清理过期数据

        新结构清理逻辑：
        - output/news/{date}.db  -> 删除过期的 .db 文件
        - output/rss/{date}.db   -> 删除过期的 .db 文件
        - output/txt/{date}/     -> 删除过期的日期目录
        - output/html/{date}/    -> 删除过期的日期目录

        Args:
            retention_days: 保留天数（0 表示不清理）

        Returns:
            删除的文件/目录数量
        """
        if retention_days <= 0:
            return 0

        deleted_count = 0
        cutoff_date = self._get_configured_time() - timedelta(days=retention_days)

        def parse_date_from_name(name: str) -> Optional[datetime]:
            """从文件名或目录名解析日期 (ISO 格式: YYYY-MM-DD)"""
            # 移除 .db 后缀
            name = name.replace('.db', '')
            try:
                date_match = re.match(r'(\d{4})-(\d{2})-(\d{2})', name)
                if date_match:
                    return datetime(
                        int(date_match.group(1)),
                        int(date_match.group(2)),
                        int(date_match.group(3)),
                        tzinfo=pytz.timezone(self.timezone)
                    )
            except Exception:
                pass
            return None

        try:
            if not self.data_dir.exists():
                return 0

            # 清理数据库文件 (news/, rss/)
            for db_type in ["news", "rss"]:
                db_dir = self.data_dir / db_type
                if not db_dir.exists():
                    continue

                for db_file in db_dir.glob("*.db"):
                    file_date = parse_date_from_name(db_file.name)
                    if file_date and file_date < cutoff_date:
                        # 先关闭数据库连接
                        db_path = str(db_file)
                        if db_path in self._db_connections:
                            try:
                                self._db_connections[db_path].close()
                                del self._db_connections[db_path]
                            except Exception:
                                pass

                        # 删除文件
                        try:
                            db_file.unlink()
                            deleted_count += 1
                            print(f"[本地存储] 清理过期数据: {db_type}/{db_file.name}")
                        except Exception as e:
                            print(f"[本地存储] 删除文件失败 {db_file}: {e}")

            # 清理快照目录 (txt/, html/)
            for snapshot_type in ["txt", "html"]:
                snapshot_dir = self.data_dir / snapshot_type
                if not snapshot_dir.exists():
                    continue

                for date_folder in snapshot_dir.iterdir():
                    if not date_folder.is_dir() or date_folder.name.startswith('.'):
                        continue

                    folder_date = parse_date_from_name(date_folder.name)
                    if folder_date and folder_date < cutoff_date:
                        try:
                            shutil.rmtree(date_folder)
                            deleted_count += 1
                            print(f"[本地存储] 清理过期数据: {snapshot_type}/{date_folder.name}")
                        except Exception as e:
                            print(f"[本地存储] 删除目录失败 {date_folder}: {e}")

            if deleted_count > 0:
                print(f"[本地存储] 共清理 {deleted_count} 个过期文件/目录")

            return deleted_count

        except Exception as e:
            print(f"[本地存储] 清理过期数据失败: {e}")
            return deleted_count

    def __del__(self):
        """析构函数，确保关闭连接"""
        self.cleanup()
