# coding=utf-8
"""TrendRadar command-line interface and operational diagnostics."""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import requests

from trendradar.context import AppContext
from trendradar import __version__
from trendradar.core import load_config
from trendradar.daily import NewsAnalyzer, _FORCE_RUN_ENV


def _parse_version(version_str: str) -> Tuple[int, int, int]:
    """解析版本号字符串为元组"""
    try:
        parts = version_str.strip().split(".")
        if len(parts) >= 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
        return 0, 0, 0
    except:
        return 0, 0, 0


def _compare_version(local: str, remote: str) -> str:
    """比较版本号，返回状态文字"""
    local_tuple = _parse_version(local)
    remote_tuple = _parse_version(remote)

    if local_tuple < remote_tuple:
        return "⚠️ 需要更新"
    elif local_tuple > remote_tuple:
        return "🔮 超前版本"
    else:
        return "✅ 已是最新"


def _fetch_remote_version(version_url: str, proxy_url: Optional[str] = None) -> Optional[str]:
    """获取远程版本号"""
    try:
        proxies = None
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/plain, */*",
            "Cache-Control": "no-cache",
        }

        response = requests.get(version_url, proxies=proxies, headers=headers, timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except Exception as e:
        print(f"[版本检查] 获取远程版本失败: {e}")
        return None


def _parse_config_versions(content: str) -> Dict[str, str]:
    """解析配置文件版本内容为字典"""
    versions = {}
    try:
        if not content:
            return versions
        for line in content.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            name, version = line.split("=", 1)
            versions[name.strip()] = version.strip()
    except Exception as e:
        print(f"[版本检查] 解析配置版本失败: {e}")
    return versions


def check_all_versions(
    version_url: str,
    configs_version_url: Optional[str] = None,
    proxy_url: Optional[str] = None
) -> Tuple[bool, Optional[str]]:
    """
    统一版本检查：程序版本 + 配置文件版本

    Args:
        version_url: 远程程序版本检查 URL
        configs_version_url: 远程配置文件版本检查 URL (返回格式: filename=version)
        proxy_url: 代理 URL

    Returns:
        (need_update, remote_version): 程序是否需要更新及远程版本号
    """
    # 获取远程版本
    remote_version = _fetch_remote_version(version_url, proxy_url)

    # 获取远程配置版本（如果有提供 URL）
    remote_config_versions = {}
    if configs_version_url:
        content = _fetch_remote_version(configs_version_url, proxy_url)
        if content:
            remote_config_versions = _parse_config_versions(content)

    print("=" * 60)
    print("版本检查")
    print("=" * 60)

    if remote_version:
        print(f"远程程序版本: {remote_version}")
    else:
        print("远程程序版本: 获取失败")

    if configs_version_url:
        if remote_config_versions:
            print(f"远程配置清单: 获取成功 ({len(remote_config_versions)} 个文件)")
        else:
            print("远程配置清单: 获取失败或为空")

    print("-" * 60)

    program_status = _compare_version(__version__, remote_version) if remote_version else "(无法比较)"
    print(f"  主程序版本: {__version__} {program_status}")

    config_files = [
        Path("config/config.yaml"),
        Path("config/timeline.yaml"),
        Path("config/frequency_words.txt"),
        Path("config/ai_interests.txt"),
        Path("config/ai_analysis_prompt.txt"),
        Path("config/ai_translation_prompt.txt"),
    ]

    version_pattern = re.compile(r"Version:\s*(\d+\.\d+\.\d+)", re.IGNORECASE)

    for config_file in config_files:
        if not config_file.exists():
            print(f"  {config_file.name}: 文件不存在")
            continue

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                local_version = None
                for i, line in enumerate(f):
                    if i >= 20:
                        break
                    match = version_pattern.search(line)
                    if match:
                        local_version = match.group(1)
                        break

                # 获取该文件的远程版本
                target_remote_version = remote_config_versions.get(config_file.name)

                if local_version:
                    if target_remote_version:
                        status = _compare_version(local_version, target_remote_version)
                        print(f"  {config_file.name}: {local_version} {status}")
                    else:
                        print(f"  {config_file.name}: {local_version} (未找到远程版本)")
                else:
                    print(f"  {config_file.name}: 未找到本地版本号")
        except Exception as e:
            print(f"  {config_file.name}: 读取失败 - {e}")

    print("=" * 60)

    # 返回程序版本的更新状态
    if remote_version:
        need_update = _parse_version(__version__) < _parse_version(remote_version)
        return need_update, remote_version if need_update else None
    return False, None

def _record_doctor_result(results: List[Tuple[str, str, str]], status: str, item: str, detail: str) -> None:
    """记录并打印 doctor 检查结果"""
    icon_map = {
        "pass": "✅",
        "warn": "⚠️",
        "fail": "❌",
    }
    icon = icon_map.get(status, "•")
    results.append((status, item, detail))
    print(f"{icon} {item}: {detail}")


def _save_doctor_report(
    results: List[Tuple[str, str, str]],
    pass_count: int,
    warn_count: int,
    fail_count: int,
    config_path: Optional[str],
) -> None:
    """保存 doctor 体检报告到 JSON 文件"""
    report = {
        "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_path": config_path or os.environ.get("CONFIG_PATH", "config/config.yaml"),
        "summary": {
            "pass": pass_count,
            "warn": warn_count,
            "fail": fail_count,
            "ok": fail_count == 0,
        },
        "checks": [
            {"status": status, "item": item, "detail": detail}
            for status, item, detail in results
        ],
    }

    try:
        output_dir = Path("output") / "meta"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "doctor_report.json"
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"体检报告已保存: {output_path}")
    except Exception as e:
        print(f"⚠️ 体检报告保存失败: {e}")


def _run_doctor(config_path: Optional[str] = None) -> bool:
    """运行环境体检"""
    print("=" * 60)
    print(f"TrendRadar v{__version__} 环境体检")
    print("=" * 60)

    results: List[Tuple[str, str, str]] = []
    config = None

    # 1) Python 版本检查
    py_ok = sys.version_info >= (3, 10)
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if py_ok:
        _record_doctor_result(results, "pass", "Python版本", f"{py_version} (满足 >= 3.10)")
    else:
        _record_doctor_result(results, "fail", "Python版本", f"{py_version} (不满足 >= 3.10)")

    # 2) 关键文件检查
    if config_path is None:
        config_path = os.environ.get("CONFIG_PATH", "config/config.yaml")

    required_files = [
        (config_path, "主配置文件"),
        ("config/frequency_words.txt", "关键词文件"),
    ]
    optional_files = [
        ("config/timeline.yaml", "调度文件"),
    ]

    for path_str, desc in required_files:
        if Path(path_str).exists():
            _record_doctor_result(results, "pass", desc, f"已找到: {path_str}")
        else:
            _record_doctor_result(results, "fail", desc, f"缺失: {path_str}")

    for path_str, desc in optional_files:
        if Path(path_str).exists():
            _record_doctor_result(results, "pass", desc, f"已找到: {path_str}")
        else:
            _record_doctor_result(results, "warn", desc, f"未找到: {path_str}（将使用默认调度模板）")

    # 3) 配置加载检查
    try:
        config = load_config(config_path)
        _record_doctor_result(results, "pass", "配置加载", f"加载成功: {config_path}")
    except Exception as e:
        _record_doctor_result(results, "fail", "配置加载", f"加载失败: {e}")

    # 后续检查依赖配置对象
    if config:
        # 4) 调度配置检查
        try:
            ctx = AppContext(config)
            schedule = ctx.create_scheduler().resolve()
            detail = f"调度解析成功（report_mode={schedule.report_mode}）"
            _record_doctor_result(results, "pass", "调度配置", detail)
        except Exception as e:
            _record_doctor_result(results, "fail", "调度配置", f"解析失败: {e}")

        # 5) AI 配置检查（按功能场景区分严重级别）
        ai_analysis_enabled = config.get("AI_ANALYSIS", {}).get("ENABLED", False)
        ai_enabled = ai_analysis_enabled

        if ai_enabled:
            try:
                from trendradar.ai.client import AIClient
                valid, message = AIClient(config.get("AI", {})).validate_config()
                if valid:
                    _record_doctor_result(results, "pass", "AI配置", f"模型: {config.get('AI', {}).get('MODEL', '')}")
                else:
                    _record_doctor_result(results, "fail", "AI配置", message)
            except Exception as e:
                _record_doctor_result(results, "fail", "AI配置", f"校验异常: {e}")
        else:
            _record_doctor_result(results, "warn", "AI配置", "未启用 AI 分析，跳过校验")

        # 6) 存储配置检查
        try:
            storage_cfg = config.get("STORAGE", {})
            backend = storage_cfg.get("BACKEND", "auto")
            remote = storage_cfg.get("REMOTE", {})
            missing_remote_keys = [
                k for k in ("BUCKET_NAME", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "ENDPOINT_URL")
                if not remote.get(k)
            ]

            if backend == "remote" and missing_remote_keys:
                _record_doctor_result(
                    results, "fail", "存储配置",
                    f"remote 模式缺少配置: {', '.join(missing_remote_keys)}"
                )
            elif backend == "auto" and os.environ.get("GITHUB_ACTIONS") == "true" and missing_remote_keys:
                _record_doctor_result(
                    results, "warn", "存储配置",
                    "GitHub Actions + auto 模式未完整配置远程存储，可能导致数据丢失"
                )
            else:
                sm = AppContext(config).get_storage_manager()
                _record_doctor_result(results, "pass", "存储配置", f"当前后端: {sm.backend_name}")
        except Exception as e:
            _record_doctor_result(results, "fail", "存储配置", f"检查失败: {e}")

        # 7) 通知渠道配置检查
        channel_details = []
        channel_issues = []
        # GA3：仅邮件
        email_ready = all(
            [
                config.get("EMAIL_FROM"),
                config.get("EMAIL_PASSWORD"),
                config.get("EMAIL_TO"),
            ]
        )
        if email_ready:
            channel_details.append("邮件")
        elif any([config.get("EMAIL_FROM"), config.get("EMAIL_PASSWORD"), config.get("EMAIL_TO")]):
            channel_issues.append("邮件配置不完整（需要 from/password/to 同时配置）")

        if channel_issues and not channel_details:
            _record_doctor_result(results, "fail", "通知配置", "；".join(channel_issues))
        elif channel_issues and channel_details:
            detail = f"可用渠道: {', '.join(channel_details)}；问题: {'；'.join(channel_issues)}"
            _record_doctor_result(results, "warn", "通知配置", detail)
        elif channel_details:
            _record_doctor_result(results, "pass", "通知配置", f"可用渠道: {', '.join(channel_details)}")
        else:
            _record_doctor_result(results, "warn", "通知配置", "未配置任何通知渠道")

        # 8) 输出目录可写检查
        try:
            output_dir = Path("output")
            output_dir.mkdir(parents=True, exist_ok=True)
            probe_file = output_dir / ".doctor_write_probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
            _record_doctor_result(results, "pass", "输出目录", f"可写: {output_dir}")
        except Exception as e:
            _record_doctor_result(results, "fail", "输出目录", f"不可写: {e}")

    pass_count = sum(1 for status, _, _ in results if status == "pass")
    warn_count = sum(1 for status, _, _ in results if status == "warn")
    fail_count = sum(1 for status, _, _ in results if status == "fail")

    _save_doctor_report(results, pass_count, warn_count, fail_count, config_path)

    print("-" * 60)
    print(f"体检结果: ✅ {pass_count} 项通过  ⚠️ {warn_count} 项警告  ❌ {fail_count} 项失败")
    print("=" * 60)

    if fail_count == 0:
        print("体检通过。")
        return True

    print("体检未通过，请先修复失败项。")
    return False


def _create_test_html_file(ctx: AppContext) -> Optional[str]:
    """创建邮件测试用 HTML 文件"""
    try:
        now = ctx.get_time()
        output_dir = Path("output") / "html" / ctx.format_date()
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / f"notification_test_{ctx.format_time()}.html"
        html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>TrendRadar 通知测试</title></head>
<body>
<h2>TrendRadar 通知连通性测试</h2>
<p>测试时间：{now.strftime('%Y-%m-%d %H:%M:%S')} ({ctx.timezone})</p>
<p>这是一条测试消息，用于验证邮件渠道是否可达。</p>
</body>
</html>"""
        html_path.write_text(html_content, encoding="utf-8")
        return str(html_path)
    except Exception as e:
        print(f"[测试通知] 创建测试 HTML 失败: {e}")
        return None


def _run_test_notification(config: Dict) -> bool:
    """发送测试通知到已配置渠道"""
    from trendradar.notification import NotificationDispatcher

    ctx = AppContext(config)

    try:
        # 检查是否配置了通知渠道
        has_notification = bool(
            config.get("EMAIL_FROM")
            and config.get("EMAIL_PASSWORD")
            and config.get("EMAIL_TO")
        )
        if not has_notification:
            print("未检测到可用通知渠道，请先在 config.yaml 或环境变量中配置。")
            return False

        dispatcher = NotificationDispatcher(
            config=config,
            get_time_func=ctx.get_time,
        )

        html_file_path = _create_test_html_file(ctx)

        print("=" * 60)
        print("通知连通性测试")
        print("=" * 60)

        results = dispatcher.dispatch_all(
            report_type="通知连通性测试",
            html_file_path=html_file_path,
        )

        if not results:
            print("没有可测试的有效通知渠道（可能配置不完整）。")
            return False

        print("-" * 60)
        success_count = 0
        for channel, ok in results.items():
            if ok:
                success_count += 1
                print(f"✅ {channel}: 测试成功")
            else:
                print(f"❌ {channel}: 测试失败")

        print("-" * 60)
        print(f"测试结果: {success_count}/{len(results)} 个渠道成功")
        return success_count > 0
    finally:
        ctx.cleanup()


def main() -> int:
    """主程序入口"""
    # 解析命令行参数
    parser = argparse.ArgumentParser(
        description="TrendRadar - 热点新闻聚合与分析工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
调度状态命令:
  --show-schedule        显示当前调度状态（时间段、行为开关）
运行命令:
  --force-run            手动强制执行 AI 分析和推送，忽略时间窗口与 once 去重
诊断命令:
  --doctor               运行环境与配置体检
  --test-notification    发送测试通知到已配置渠道

示例:
  python -m trendradar                    # 正常运行
  python -m trendradar --force-run        # 手动强制运行并推送
  python -m trendradar --show-schedule    # 查看当前调度状态
  python -m trendradar --doctor           # 运行一键体检
  python -m trendradar --test-notification # 测试通知渠道连通性
"""
    )
    parser.add_argument(
        "--force-run",
        action="store_true",
        help="手动强制执行 AI 分析和推送，忽略时间窗口与 once 去重"
    )
    parser.add_argument(
        "--show-schedule",
        action="store_true",
        help="显示当前调度状态"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="运行环境与配置体检"
    )
    parser.add_argument(
        "--test-notification",
        action="store_true",
        help="发送测试通知到已配置渠道"
    )

    args = parser.parse_args()

    debug_mode = False
    try:
        if args.force_run:
            os.environ[_FORCE_RUN_ENV] = "1"
            print("[手动运行] 已启用强制模式：忽略分析/推送时间窗口与 once 去重")

        # 处理 doctor 命令（不依赖完整运行流程）
        if args.doctor:
            ok = _run_doctor()
            if not ok:
                raise SystemExit(1)
            return 0

        # 先加载配置
        config = load_config()

        # 处理状态查看命令
        if args.show_schedule:
            _handle_status_commands(config)
            return 0

        # 处理通知测试命令
        if args.test_notification:
            ok = _run_test_notification(config)
            if not ok:
                raise SystemExit(1)
            return 0

        version_url = config.get("VERSION_CHECK_URL", "")
        configs_version_url = config.get("CONFIGS_VERSION_CHECK_URL", "")

        # 统一版本检查（程序版本 + 配置文件版本，只请求一次远程）
        need_update = False
        remote_version = None
        if version_url:
            need_update, remote_version = check_all_versions(version_url, configs_version_url)

        # 复用已加载的配置，避免重复加载
        analyzer = NewsAnalyzer(config=config)

        # 设置更新信息（复用已获取的远程版本，不再重复请求）
        if analyzer.is_github_actions and need_update and remote_version:
            analyzer.update_info = {
                "current_version": __version__,
                "remote_version": remote_version,
            }

        # 获取 debug 配置
        debug_mode = analyzer.ctx.config.get("DEBUG", False)
        analyzer.run()
        return 0
    except FileNotFoundError as e:
        print(f"❌ 配置文件错误: {e}")
        print("\n请确保以下文件存在:")
        print("  • config/config.yaml")
        print("  • config/frequency_words.txt")
        print("\n参考项目文档进行正确配置")
        return 1
    except Exception as e:
        print(f"❌ 程序运行错误: {e}")
        if debug_mode:
            raise
        return 1


def _handle_status_commands(config: Dict) -> None:
    """处理状态查看命令 - 显示当前调度状态"""
    from trendradar.context import AppContext

    ctx = AppContext(config)

    print("=" * 60)
    print(f"TrendRadar v{__version__} 调度状态")
    print("=" * 60)

    try:
        scheduler = ctx.create_scheduler()
        schedule = scheduler.resolve()

        now = ctx.get_time()
        date_str = ctx.format_date()

        print(f"\n⏰ 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} ({ctx.timezone})")
        print(f"📅 当前日期: {date_str}")

        print(f"\n📋 调度信息:")
        print(f"  日计划: {schedule.day_plan}")
        if schedule.period_key:
            print(f"  当前时间段: {schedule.period_name or schedule.period_key} ({schedule.period_key})")
        else:
            print(f"  当前时间段: 无（使用默认配置）")

        print(f"\n🔧 行为开关:")
        print(f"  采集数据: {'✅ 是' if schedule.collect else '❌ 否'}")
        print(f"  AI 分析:  {'✅ 是' if schedule.analyze else '❌ 否'}")
        print(f"  推送通知: {'✅ 是' if schedule.push else '❌ 否'}")
        print(f"  报告模式: {schedule.report_mode}")

        if schedule.period_key:
            print(f"\n🔁 一次性控制:")
            if schedule.once_analyze:
                already_analyzed = scheduler.already_executed(schedule.period_key, "analyze", date_str)
                print(f"  AI 分析:  仅一次 {'(今日已执行 ⚠️)' if already_analyzed else '(今日未执行 ✅)'}")
            else:
                print(f"  AI 分析:  不限次数")
            if schedule.once_push:
                already_pushed = scheduler.already_executed(schedule.period_key, "push", date_str)
                print(f"  推送通知: 仅一次 {'(今日已执行 ⚠️)' if already_pushed else '(今日未执行 ✅)'}")
            else:
                print(f"  推送通知: 不限次数")

    except Exception as e:
        print(f"\n❌ 获取调度状态失败: {e}")

    print("\n" + "=" * 60)

    # 清理资源
    ctx.cleanup()
