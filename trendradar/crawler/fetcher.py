# coding=utf-8
"""
数据获取器模块

负责从 NewsNow API 抓取新闻数据，支持：
- 单个平台数据获取
- 批量平台数据爬取
- 自动重试机制
- 主源失败后按序 fallback 到备用 API
- 代理支持
"""

import json
import random
import time
from typing import Dict, List, Tuple, Optional, Union
from urllib.parse import urlparse

import requests

from trendradar.utils.text import html_to_plain_text


class DataFetcher:
    """数据获取器"""

    # 默认 API 地址
    DEFAULT_API_URL = "https://newsnow.busiyi.world/api/s"

    # 默认请求头
    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        api_url: Optional[str] = None,
        api_fallback_urls: Optional[List[str]] = None,
    ):
        """
        初始化数据获取器

        Args:
            proxy_url: 代理服务器 URL（可选）
            api_url: API 基础 URL（可选，默认使用 DEFAULT_API_URL）
            api_fallback_urls: 备用 API 列表；主源重试耗尽后按序尝试
        """
        self.proxy_url = proxy_url
        primary = (api_url or "").strip() or self.DEFAULT_API_URL
        primary = primary.rstrip("/")
        if "?" in primary:
            primary = primary.split("?", 1)[0].rstrip("/")

        fallbacks: List[str] = []
        for raw in api_fallback_urls or []:
            url = (raw or "").strip().rstrip("/")
            if not url:
                continue
            if "?" in url:
                url = url.split("?", 1)[0].rstrip("/")
            if url != primary and url not in fallbacks:
                fallbacks.append(url)

        self.api_url = primary
        self.api_urls = [self.api_url] + fallbacks

    @staticmethod
    def _check_domain_safety(items: List[Dict], expected_domain: str) -> bool:
        """验证新闻链接仅使用预期域名的 HTTPS 地址。"""
        expected = expected_domain.strip().lower().rstrip(".")
        if not expected:
            return True

        for item in items:
            if not isinstance(item, dict):
                print("域名安全检查失败: 新闻条目格式无效")
                return False

            for field in ("url", "mobileUrl"):
                raw_url = item.get(field)
                if not raw_url:
                    continue

                try:
                    parsed = urlparse(str(raw_url).strip())
                    hostname = (parsed.hostname or "").lower().rstrip(".")
                except (TypeError, ValueError, UnicodeError):
                    print(f"域名安全检查失败: {field} 不是有效 URL")
                    return False

                if parsed.scheme.lower() != "https":
                    print(f"域名安全检查失败: {field} 不是 HTTPS")
                    return False

                if hostname != expected and not hostname.endswith(f".{expected}"):
                    print(
                        f"域名安全检查失败: {field} 域名 {hostname or '<empty>'} "
                        f"不属于 {expected}"
                    )
                    return False

        return True

    def fetch_data(
        self,
        id_info: Union[str, Tuple[str, str]],
        max_retries: int = 2,
        min_retry_wait: int = 3,
        max_retry_wait: int = 5,
    ) -> Tuple[Optional[str], str, str]:
        """
        获取指定ID数据，支持重试与 API fallback

        Args:
            id_info: 平台ID 或 (平台ID, 别名) 元组
            max_retries: 主源最大重试次数
            min_retry_wait: 最小重试等待时间（秒）
            max_retry_wait: 最大重试等待时间（秒）

        Returns:
            (响应文本, 平台ID, 别名) 元组，失败时响应文本为 None
        """
        if isinstance(id_info, tuple):
            id_value, alias = id_info
        else:
            id_value = id_info
            alias = id_value

        proxies = None
        if self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}

        total_bases = len(self.api_urls)

        for base_index, api_base in enumerate(self.api_urls):
            url = f"{api_base}?id={id_value}&latest"
            source_label = "主源" if base_index == 0 else f"fallback#{base_index}"
            retries = 0
            # fallback 少重试一次，避免拖垮整轮爬取
            retries_limit = max_retries if base_index == 0 else max(0, max_retries - 1)

            while retries <= retries_limit:
                try:
                    response = requests.get(
                        url,
                        proxies=proxies,
                        headers=self.DEFAULT_HEADERS,
                        timeout=10,
                    )
                    response.raise_for_status()

                    data_text = response.text
                    data_json = json.loads(data_text)

                    status = data_json.get("status", "未知")
                    if status not in ["success", "cache"]:
                        raise ValueError(f"响应状态异常: {status}")

                    status_info = "最新数据" if status == "success" else "缓存数据"
                    if base_index == 0:
                        print(f"获取 {id_value} 成功（{status_info}）")
                    else:
                        print(
                            f"获取 {id_value} 成功（{status_info}，经 {source_label}: {api_base}）"
                        )
                    return data_text, id_value, alias

                except Exception as e:
                    retries += 1
                    if retries <= retries_limit:
                        base_wait = random.uniform(min_retry_wait, max_retry_wait)
                        additional_wait = (retries - 1) * random.uniform(1, 2)
                        wait_time = base_wait + additional_wait
                        print(
                            f"请求 {id_value} 失败[{source_label}]: {e}. "
                            f"{wait_time:.2f}秒后重试..."
                        )
                        time.sleep(wait_time)
                    elif base_index + 1 < total_bases:
                        next_base = self.api_urls[base_index + 1]
                        print(
                            f"请求 {id_value} 主路径耗尽[{source_label}]: {e}；"
                            f"切换 fallback -> {next_base}"
                        )
                    else:
                        print(f"请求 {id_value} 失败: {e}")

        return None, id_value, alias

    def crawl_websites(
        self,
        ids_list: List[Union[str, Tuple[str, str]]],
        request_interval: int = 100,
        domain_rules: Optional[Dict[str, str]] = None,
    ) -> Tuple[Dict, Dict, List]:
        """
        爬取多个网站数据

        Args:
            ids_list: 平台ID列表，每个元素可以是字符串或 (平台ID, 别名) 元组
            request_interval: 请求间隔（毫秒）
            domain_rules: 平台 ID 到预期域名的映射；未配置的平台不校验

        Returns:
            (结果字典, ID到名称的映射, 失败ID列表) 元组
        """
        results = {}
        id_to_name = {}
        failed_ids = []
        domain_rules = domain_rules or {}

        for i, id_info in enumerate(ids_list):
            if isinstance(id_info, tuple):
                id_value, name = id_info
            else:
                id_value = id_info
                name = id_value

            id_to_name[id_value] = name
            response, _, _ = self.fetch_data(id_info)

            if response:
                try:
                    data = json.loads(response)
                    items = data.get("items", [])
                    expected_domain = domain_rules.get(id_value, "")

                    if expected_domain and not self._check_domain_safety(
                        items, expected_domain
                    ):
                        print(f"域名安全检查失败，丢弃 {id_value} 本次数据")
                        failed_ids.append(id_value)
                    else:
                        results[id_value] = {}
                        cleaned_title_count = 0

                        for index, item in enumerate(items, 1):
                            title = item.get("title")
                            # 跳过无效标题（None、float、空字符串）
                            if title is None or isinstance(title, float):
                                continue

                            raw_title = str(title).strip()
                            title = html_to_plain_text(raw_title)
                            if not title:
                                continue
                            if title != raw_title:
                                cleaned_title_count += 1

                            url = item.get("url", "")
                            mobile_url = item.get("mobileUrl", "")

                            if title in results[id_value]:
                                results[id_value][title]["ranks"].append(index)
                            else:
                                results[id_value][title] = {
                                    "ranks": [index],
                                    "url": url,
                                    "mobileUrl": mobile_url,
                                }

                        if cleaned_title_count:
                            print(
                                f"已清理 {id_value} 的 {cleaned_title_count} 条"
                                "标题富文本或异常空白"
                            )
                except json.JSONDecodeError:
                    print(f"解析 {id_value} 响应失败")
                    failed_ids.append(id_value)
                except Exception as e:
                    print(f"处理 {id_value} 数据出错: {e}")
                    failed_ids.append(id_value)
            else:
                failed_ids.append(id_value)

            # 请求间隔（除了最后一个）
            if i < len(ids_list) - 1:
                actual_interval = request_interval + random.randint(-10, 20)
                actual_interval = max(50, actual_interval)
                time.sleep(actual_interval / 1000)

        print(f"成功: {list(results.keys())}, 失败: {failed_ids}")
        return results, id_to_name, failed_ids
