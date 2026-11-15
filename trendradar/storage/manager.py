# coding=utf-8
"""
存储管理器 - 统一管理存储后端

根据环境和配置自动选择合适的存储后端
"""

import os
from pathlib import Path
from typing import Optional

from trendradar.storage.base import StorageBackend, NewsData, RSSData
from trendradar.storage.day_snapshot import DaySnapshotSource
from trendradar.utils.time import DEFAULT_TIMEZONE


# 存储管理器单例
_storage_manager: Optional["StorageManager"] = None


class StorageBackendError(RuntimeError):
    """A required storage backend could not be selected or initialized."""


def storage_manager_options(config: dict, *, project_root: str | Path | None = None) -> dict:
    """Map the application's normalized configuration once for all entrypoints."""
    storage = config.get("STORAGE", {})
    remote = storage.get("REMOTE", {})
    local = storage.get("LOCAL", {})
    pull = storage.get("PULL", {})
    formats = storage.get("FORMATS", {})
    data_dir = local.get("DATA_DIR", "output")
    if project_root is not None:
        path = Path(data_dir)
        data_dir = str(path if path.is_absolute() else Path(project_root) / path)
    return {
        "backend_type": storage.get("BACKEND", "auto"),
        "data_dir": data_dir,
        "enable_txt": formats.get("TXT", True),
        "remote_config": {
            "bucket_name": remote.get("BUCKET_NAME", ""),
            "access_key_id": remote.get("ACCESS_KEY_ID", ""),
            "secret_access_key": remote.get("SECRET_ACCESS_KEY", ""),
            "endpoint_url": remote.get("ENDPOINT_URL", ""),
            "region": remote.get("REGION", ""),
        },
        "local_retention_days": local.get("RETENTION_DAYS", 0),
        "remote_retention_days": remote.get("RETENTION_DAYS", 0),
        "pull_enabled": pull.get("ENABLED", False),
        "pull_days": pull.get("DAYS", 7),
        "timezone": config.get("TIMEZONE", DEFAULT_TIMEZONE),
    }


class StorageManager:
    """
    存储管理器

    功能：
    - 自动检测运行环境（GitHub Actions / 本地）
    - 根据配置选择存储后端（local / remote / auto）
    - 提供统一的存储接口
    - 支持从远程拉取数据到本地
    """

    def __init__(
        self,
        backend_type: str = "auto",
        data_dir: str = "output",
        enable_txt: bool = True,
        remote_config: Optional[dict] = None,
        local_retention_days: int = 0,
        remote_retention_days: int = 0,
        pull_enabled: bool = False,
        pull_days: int = 0,
        timezone: str = DEFAULT_TIMEZONE,
    ):
        """
        初始化存储管理器

        Args:
            backend_type: 存储后端类型 (local / remote / auto)
            data_dir: 本地数据目录
            enable_txt: 是否启用 TXT 快照
            remote_config: 远程存储配置（endpoint_url, bucket_name, access_key_id 等）
            local_retention_days: 本地数据保留天数（0 = 无限制）
            remote_retention_days: 远程数据保留天数（0 = 无限制）
            pull_enabled: 是否启用启动时自动拉取
            pull_days: 拉取最近 N 天的数据
            timezone: 时区配置
        """
        if backend_type not in {"local", "remote", "auto"}:
            raise ValueError("Storage backend must be local, remote or auto")
        self.backend_type = backend_type
        self.data_dir = data_dir
        self.enable_txt = enable_txt
        self.remote_config = remote_config or {}
        self.local_retention_days = local_retention_days
        self.remote_retention_days = remote_retention_days
        self.pull_enabled = pull_enabled
        self.pull_days = pull_days
        self.timezone = timezone

        self._backend: Optional[StorageBackend] = None
        self._remote_backend: Optional[StorageBackend] = None
        self._day_source: Optional[DaySnapshotSource] = None

    @classmethod
    def from_config(cls, config: dict, *, project_root: str | Path | None = None) -> "StorageManager":
        """Build an independently owned manager, without a process-global pin."""
        return cls(**storage_manager_options(config, project_root=project_root))

    @staticmethod
    def is_github_actions() -> bool:
        """检测是否在 GitHub Actions 环境中运行"""
        return os.environ.get("GITHUB_ACTIONS") == "true"


    def _resolve_backend_type(self) -> str:
        """解析实际使用的后端类型"""
        if self.backend_type == "auto":
            if self.is_github_actions():
                # GitHub Actions 环境，检查是否配置了远程存储
                if self._has_remote_config():
                    return "remote"
                else:
                    print("[存储管理器] GitHub Actions 环境但未配置远程存储，使用本地存储")
                    return "local"
            else:
                return "local"
        return self.backend_type

    def _has_remote_config(self) -> bool:
        """检查是否有有效的远程存储配置"""
        # 检查配置或环境变量
        bucket_name = self.remote_config.get("bucket_name") or os.environ.get("S3_BUCKET_NAME")
        access_key = self.remote_config.get("access_key_id") or os.environ.get("S3_ACCESS_KEY_ID")
        secret_key = self.remote_config.get("secret_access_key") or os.environ.get("S3_SECRET_ACCESS_KEY")
        endpoint = self.remote_config.get("endpoint_url") or os.environ.get("S3_ENDPOINT_URL")

        # 调试日志
        has_config = bool(bucket_name and access_key and secret_key and endpoint)
        if not has_config:
            print(f"[存储管理器] 远程存储配置检查失败:")
            print(f"  - bucket_name: {'已配置' if bucket_name else '未配置'}")
            print(f"  - access_key_id: {'已配置' if access_key else '未配置'}")
            print(f"  - secret_access_key: {'已配置' if secret_key else '未配置'}")
            print(f"  - endpoint_url: {'已配置' if endpoint else '未配置'}")

        return has_config

    def _create_remote_backend(self) -> Optional[StorageBackend]:
        """创建远程存储后端"""
        try:
            from trendradar.storage.remote import RemoteStorageBackend

            return RemoteStorageBackend(
                bucket_name=self.remote_config.get("bucket_name") or os.environ.get("S3_BUCKET_NAME", ""),
                access_key_id=self.remote_config.get("access_key_id") or os.environ.get("S3_ACCESS_KEY_ID", ""),
                secret_access_key=self.remote_config.get("secret_access_key") or os.environ.get("S3_SECRET_ACCESS_KEY", ""),
                endpoint_url=self.remote_config.get("endpoint_url") or os.environ.get("S3_ENDPOINT_URL", ""),
                region=self.remote_config.get("region") or os.environ.get("S3_REGION", ""),
                enable_txt=self.enable_txt,
                timezone=self.timezone,
            )
        except ImportError as e:
            print(f"[存储管理器] 远程后端导入失败: {type(e).__name__}")
            print("[存储管理器] 请确保已安装 boto3: pip install boto3")
            return None
        except Exception as e:
            print(f"[存储管理器] 远程后端初始化失败: {type(e).__name__}")
            return None

    def get_backend(self) -> StorageBackend:
        """Select once; a selected remote backend must never become local."""
        if self._backend is None:
            resolved_type = self._resolve_backend_type()

            if resolved_type == "remote":
                self._backend = self._create_remote_backend()
                if self._backend is None or self._backend.backend_name != "remote":
                    self._backend = None
                    raise StorageBackendError("Cannot initialize required remote storage backend")
                print("[存储管理器] 使用远程存储后端")
            else:
                from trendradar.storage.local import LocalStorageBackend

                self._backend = LocalStorageBackend(
                    data_dir=self.data_dir,
                    enable_txt=self.enable_txt,
                    timezone=self.timezone,
                )
                print(f"[存储管理器] 使用本地存储后端 (数据目录: {self.data_dir})")
        if self.backend_type == "remote" and self._backend.backend_name != "remote":
            raise StorageBackendError("Required remote storage cannot use a local backend")
        return self._backend

    def get_day_source(self) -> DaySnapshotSource:
        """Share one read-only source and the selected backend's borrowed client."""
        backend = self.get_backend()
        if self._resolve_backend_type() == "remote" and backend.backend_name != "remote":
            raise StorageBackendError("Remote source cannot use a local backend")
        if self._day_source is None:
            self._day_source = DaySnapshotSource(backend)
        return self._day_source

    def pull_from_remote(self) -> int:
        """
        从远程拉取数据到本地

        Returns:
            成功拉取的文件数量
        """
        if not self.pull_enabled or self.pull_days <= 0:
            return 0

        if not self._has_remote_config():
            print("[存储管理器] 未配置远程存储，无法拉取")
            return 0

        # 创建远程后端（如果还没有）
        if self._remote_backend is None:
            self._remote_backend = self._create_remote_backend()

        if self._remote_backend is None:
            print("[存储管理器] 无法创建远程后端，拉取失败")
            return 0

        # 调用拉取方法
        return self._remote_backend.pull_recent_days(self.pull_days, self.data_dir)

    def save_news_data(self, data: NewsData) -> bool:
        """保存新闻数据"""
        return self.get_backend().save_news_data(data)

    def save_rss_data(self, data: RSSData) -> bool:
        """保存 RSS 数据"""
        return self.get_backend().save_rss_data(data)


    def save_txt_snapshot(self, data: NewsData) -> Optional[str]:
        """保存 TXT 快照"""
        return self.get_backend().save_txt_snapshot(data)


    def cleanup(self) -> None:
        """清理资源；不执行数据库保留策略或删除业务数据。"""
        self._day_source = None
        if self._backend:
            self._backend.cleanup()
        if self._remote_backend and self._remote_backend is not self._backend:
            self._remote_backend.cleanup()

    def cleanup_old_data(self) -> int:
        """
        清理过期数据

        Returns:
            删除的日期目录数量
        """
        total_deleted = 0

        # 清理本地数据
        if self.local_retention_days > 0:
            total_deleted += self.get_backend().cleanup_old_data(self.local_retention_days)

        # 清理远程数据（如果配置了）
        if self.remote_retention_days > 0 and self._has_remote_config():
            if self._remote_backend is None:
                self._remote_backend = self._create_remote_backend()
            if self._remote_backend:
                total_deleted += self._remote_backend.cleanup_old_data(self.remote_retention_days)

        return total_deleted

    @property
    def backend_name(self) -> str:
        """获取当前后端名称"""
        return self.get_backend().backend_name

    @property
    def supports_txt(self) -> bool:
        """是否支持 TXT 快照"""
        return self.get_backend().supports_txt

    def has_period_executed(self, date_str: str, period_key: str, action: str) -> bool:
        """检查指定时间段的某个 action 是否已执行"""
        return self.get_backend().has_period_executed(date_str, period_key, action)

    def record_period_execution(self, date_str: str, period_key: str, action: str) -> bool:
        """记录时间段的 action 执行"""
        return self.get_backend().record_period_execution(date_str, period_key, action)

    # === AI 智能筛选存储操作 ===

    def begin_batch(self):
        """开启批量模式（远程后端延迟上传）"""
        self.get_backend().begin_batch()

    def end_batch(self):
        """结束批量模式（统一上传脏数据库）"""
        return self.get_backend().end_batch()

    def flush(self) -> bool:
        """同步批量模式下积累的变更，不结束批量模式。"""
        return self.get_backend().flush()


def get_storage_manager(
    backend_type: str = "auto",
    data_dir: str = "output",
    enable_txt: bool = True,
    remote_config: Optional[dict] = None,
    local_retention_days: int = 0,
    remote_retention_days: int = 0,
    pull_enabled: bool = False,
    pull_days: int = 0,
    timezone: str = DEFAULT_TIMEZONE,
    force_new: bool = False,
) -> StorageManager:
    """
    获取存储管理器单例

    Args:
        backend_type: 存储后端类型
        data_dir: 本地数据目录
        enable_txt: 是否启用 TXT 快照
        remote_config: 远程存储配置
        local_retention_days: 本地数据保留天数（0 = 无限制）
        remote_retention_days: 远程数据保留天数（0 = 无限制）
        pull_enabled: 是否启用启动时自动拉取
        pull_days: 拉取最近 N 天的数据
        timezone: 时区配置
        force_new: 是否强制创建新实例

    Returns:
        StorageManager 实例
    """
    global _storage_manager

    if _storage_manager is None or force_new:
        _storage_manager = StorageManager(
            backend_type=backend_type,
            data_dir=data_dir,
            enable_txt=enable_txt,
            remote_config=remote_config,
            local_retention_days=local_retention_days,
            remote_retention_days=remote_retention_days,
            pull_enabled=pull_enabled,
            pull_days=pull_days,
            timezone=timezone,
        )

    return _storage_manager
