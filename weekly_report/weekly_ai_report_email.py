#!/usr/bin/env python3
"""Weekly AI report entry for GitHub Actions.

选稿：合并→聚簇→打分→热榜/RSS 动态配额；正文输出主题证据，关键词失败时走独立回退。
提示词：config/weekly_ai_prompt.txt、config/weekly_keyword_prompt.txt。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trendradar.storage.history_reader import HistoryReader
from trendradar.ai.client import AIClient, build_keyword_client
from trendradar.core.loader import load_ai_config
from trendradar.notification.senders import send_to_email

OUTPUT_DIR = PROJECT_ROOT / "output" / "weekly-ai-reports"
WEEKLY_AI_PROMPT_FILE = PROJECT_ROOT / "config" / "weekly_ai_prompt.txt"
WEEKLY_KEYWORD_PROMPT_FILE = PROJECT_ROOT / "config" / "weekly_keyword_prompt.txt"

# 周报参数（以后要调直接改这里）
MAX_NEWS = 240
SIM_THRESHOLD = 0.72
RSS_TARGET_RATIO = 0.15
RSS_MAX_RATIO = 0.20
RSS_NEWS_MATCH_THRESHOLD = 0.58
KEYWORD_TOPN = 5
KEYWORD_TITLE_LIMIT = 100
HEADLINE_MIN_LEN = 2
HEADLINE_MAX_LEN = 4

# 核心三维（默认与 config.yaml advanced.weight 同口径；启动时可被 yaml 覆盖）
WEIGHT_RANK = 0.6
WEIGHT_FREQUENCY = 0.3
WEIGHT_HOTNESS = 0.1
# 周尺度附加项：与三维分开，不从 yaml 拿
WEIGHT_SPAN = 0.10
WEIGHT_PLATFORM = 0.05
# 三维在热榜总分中的占比（剩余给 span+platform）
CORE_WEIGHT_SHARE = 0.85
# RSS：freq/span 两项
RSS_WEIGHT_FREQUENCY = 0.60
RSS_WEIGHT_SPAN = 0.40
# 缺库时按可用天数缩 RSS 配额（不硬塞 20%）
RSS_SCALE_BY_AVAILABLE_DAYS = True
# 软降权（不删除，只打压热度顶前）
SOFT_TOPIC_PENALTY = 0.45
SOFT_FORMAT_PENALTY = 0.55
COLUMN_PENALTY = 0.08

STOPWORDS = {
    "今日", "本周", "最新", "热点", "表示", "消息", "中国", "美国", "公司", "市场", "已经", "进行", "相关", "发布", "报道", "工作", "记者",
    "快讯", "图示", "数据", "显示", "回应", "通报", "指出", "视频", "全文", "直播", "热搜", "话题", "头条", "网友", "全球", "国内", "国际",
    "同比", "环比", "财经", "财联社", "金十", "金十数据", "卫报", "BBC", "雅虎", "新华社", "中新网", "局势", "政策", "基建",
}

NOISE_WORDS = {
    "亿元", "美元", "万亿", "万亿元", "万元", "百亿", "千亿", "图示", "快讯", "消息", "市场消息", "金十图示", "最新动态", "边打边",
}

# 邮件头禁止/空泛词（方案 B）
HEADLINE_BANNED = {
    "亿元", "美元", "市场消息", "金十图示", "图示", "快讯", "热点", "新闻", "边打边", "最新动态",
    "中东局势", "AI基建", "特朗普政策", "全球市场", "避险情绪", "中国消费", "智能终端",
    "局势", "政策", "市场", "基建", "消息", "动态", "分析", "观察", "综述",
    # 真实周报规则兜底中出现过的空泛词与滑窗碎片
    "规模", "科技", "早盘", "盘收", "如何", "为何", "怎么", "哪些", "上涨", "发行", "最高", "年期",
    "利率", "倍数", "登陆", "风白海", "宇树科", "发行利率", "边际倍数",
    "早盘收", "风白", "倍数预",
}

# 债券发行行情是高频结构化数据，不适合从字符滑窗中提取周报主题。
# 整条标题跳过，避免完整词被过滤后又留下“行利/际倍”一类内部碎片。
RULE_DATA_TEMPLATE_TITLE_RE = re.compile(
    r"(发行利率|边际利率|投标倍数|边际倍数|倍数预期)"
)

# 允许略长的缩写/专名
HEADLINE_LEN_EXCEPTIONS = {
    "CPI", "GDP", "GPU", "Nvidia", "OpenAI", "ChatGPT", "WTI", "OPEC", "VIX",
}

# 栏目/模板帖：每日固定输出，跨天频次虚高，选稿与关键词都要压
COLUMN_TITLE_RE = re.compile(
    r"("
    r"早餐|早报|晚报|午报|FM-?Radio|电台|"
    r"金十图示|金十数据整理|持仓报告|ETF持仓|CFTC|"
    r"每日人工智能动态|每日汇总|动态汇总|局势跟踪|"
    r"24小时|最新24小时|欢迎点击查看|点击查看>>|"
    r"收盘综述|盘中速递|行情复盘|数据一览"
    r")",
    re.I,
)
DATE_IN_TITLE_RE = re.compile(
    r"("
    r"20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2}日?|"
    r"\d{1,2}月\d{1,2}日|"
    r"[（(]?\d{1,2}[-/.]\d{1,2}[)）]?|"
    r"[（(]20\d{2}[-/]\d{1,2}[-/]\d{1,2}[)）]"
    r")"
)
# 软格式噪音（轻于栏目，不删，仅打压排名）
SOFT_FORMAT_TITLE_RE = re.compile(
    r"("
    r"马上评|数说中国|图解|一图读懂|"
    r"热榜解读|热点追踪|今日话题|网友热议|"
    r"深度解读|专家解读|点击查看详情"
    r")",
    re.I,
)
# 纯体育/娱乐高热（无硬新闻锚点时轻降权）——针对「詹姆斯加盟76人」类
SOFT_TOPIC_RE = re.compile(
    r"("
    # 体育/娱乐实体或明确语境；不用裸「加盟/签约」以免误伤商业加盟
    r"詹姆斯|LeBron|NBA|CBA|篮球|足球|世界杯|欧冠|奥运会|"
    r"中超|中甲|甲A|英超|西甲|意甲|德甲|法甲|"
    r"球星|球队|球员|教练|总冠军|季后赛|"
    r"(?:球星|球队|球员|篮球|足球|NBA|CBA).{0,6}(?:加盟|转会|签约)|"
    r"(?:加盟|转会|签约).{0,6}(?:球星|球队|球员|篮球|足球|NBA|CBA)|"
    r"明星|娱乐圈|综艺|电影|剧集|演唱会|粉丝|追星|"
    r"婚礼|离婚|恋情|出轨|封杀|娱乐新闻"
    r")",
    re.I,
)
# 硬新闻锚点：有这些时不对体育/娱乐做软降权
HARD_NEWS_ANCHOR_RE = re.compile(
    r"("
    r"证监会|发改委|国务院|政治局|中央|部委|外交部|国防部|"
    r"罚没|罚款|被查|纪委|反垄断|垄断|监管|合规|"
    r"上市|股票|股市|资本市场|美联储|利率|汇率|CPI|GDP|"
    r"半导体|芯片|存储|产能|产业链|制造|科技|"
    r"地震|台风|崩塌|洪灾|灾害|疫情|战争|军演|地缘|"
    r"伊朗|以色列|中东|美军|北约|联合国|制裁"
    r")",
    re.I,
)
RSS_NOISE_RE = re.compile(
    r"(best\s+(?:credit\s+cards?|cd\s+rates?|savings\s+accounts?|mortgage\s+rates?)|"
    r"credit\s+card|refinance\s+(?:interest\s+)?rates?|apy\b|"
    r"watch:\s|bystander\s+video|visitors?\s+react|"
    r"impaled|horse\s+statues?|travel\s+rewards?|vacations?|"
    r"analyst\s+report|earnings\s+call|stock\s+fans|reasons?\s+to\s+buy|"
    r"prices?\s+today|price\s+trends?|mark\s+your\s+calendars?|"
    r"soybeans?\s+(?:collapse|rally)|weather\s+and\s+outside\s+pressures?)",
    re.I,
)
RSS_HARD_NEWS_RE = re.compile(
    r"(war|strike|attack|ceasefire|sanction|election|government|court|"
    r"earthquake|typhoon|flood|wildfire|disaster|killed|deaths?|"
    r"central\s+bank|interest\s+rate|inflation|gdp|chip|semiconductor|\bai\b|"
    r"战争|袭击|停火|制裁|选举|政府|法院|地震|台风|洪灾|山火|灾害|"
    r"遇难|央行|利率|通胀|芯片|半导体|人工智能|政治局|外交部|军演)",
    re.I,
)


def _load_core_weights_from_config() -> None:
    """可选：从 config.yaml advanced.weight 同步核心三维；周尺度附加项不动。"""
    global WEIGHT_RANK, WEIGHT_FREQUENCY, WEIGHT_HOTNESS
    cfg_path = PROJECT_ROOT / "config" / "config.yaml"
    if not cfg_path.exists():
        return
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        weight = ((data.get("advanced") or {}).get("weight") or {})
        rank = float(weight.get("rank", WEIGHT_RANK))
        freq = float(weight.get("frequency", WEIGHT_FREQUENCY))
        hot = float(weight.get("hotness", WEIGHT_HOTNESS))
        total = rank + freq + hot
        if total <= 0:
            return
        # 归一化，避免 yaml 三项不和为 1
        WEIGHT_RANK = rank / total
        WEIGHT_FREQUENCY = freq / total
        WEIGHT_HOTNESS = hot / total
    except Exception as exc:
        print(f"[weight] load config.yaml failed, keep defaults: {exc}")


_load_core_weights_from_config()


def _load_prompt_template(prompt_path: Path) -> Tuple[str, str]:
    """加载提示词；仅识别单独成行的 [system] / [user]，缺文件或 user 空则硬失败。"""
    if not prompt_path.exists():
        raise FileNotFoundError(f"周报提示词不存在: {prompt_path}")
    lines = prompt_path.read_text(encoding="utf-8").splitlines()
    sections: Dict[str, List[str]] = {"system": [], "user": []}
    current: Optional[str] = None
    for raw in lines:
        token = raw.strip()
        if token == "[system]":
            current = "system"
            continue
        if token == "[user]":
            current = "user"
            continue
        if current:
            sections[current].append(raw)
    system_prompt = "\n".join(sections["system"]).strip()
    user_prompt = "\n".join(sections["user"]).strip()
    if not user_prompt:
        raise ValueError(f"周报提示词 user 段为空: {prompt_path}")
    return system_prompt, user_prompt


def _fill_prompt_template(template: str, mapping: Dict[str, str]) -> str:
    """按占位符替换；未知花括号保持原样。"""
    out = template
    for key, value in mapping.items():
        out = out.replace("{" + key + "}", value)
    return out


def load_runtime_env() -> Dict[str, str]:
    """仅从进程环境变量读取 EMAIL_*（AI 配置走 load_ai_config）。"""
    data: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key.startswith("EMAIL_"):
            data[key] = value
    return data


def resolve_date_range(start: Optional[str], end: Optional[str]) -> Tuple[datetime, datetime]:
    if bool(start) ^ bool(end):
        raise SystemExit("--start 和 --end 必须一起传")
    if start and end:
        start_date = datetime.strptime(start, "%Y-%m-%d")
        end_date = datetime.strptime(end, "%Y-%m-%d")
        if start_date > end_date:
            raise SystemExit("--start 不能晚于 --end")
        return start_date, end_date
    end_date = datetime.now()
    start_date = end_date - timedelta(days=6)
    return start_date, end_date


def normalize_title(title: str) -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    # 轻度规范化：去首尾装饰性括号/标点，便于精确合并
    text = re.sub(r"^[\s\[【（(]+|[\]】）)\s]+$", "", text)
    text = re.sub(r"[！!？?。．.]+$", "", text)
    return text.strip()


def strip_title_dates(title: str) -> str:
    """去掉标题中的日期碎片，便于跨日栏目/同事件归并。"""
    text = DATE_IN_TITLE_RE.sub(" ", title or "")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[|｜/／:：\-—]+$", "", text).strip(" |｜/-—")
    return text.strip() or (title or "").strip()


def is_column_title(title: str) -> bool:
    t = title or ""
    if not t:
        return False
    if COLUMN_TITLE_RE.search(t):
        return True
    # 纯模板尾巴
    if re.search(r"欢迎点击|点击查看|在金十数据中心更新", t):
        return True
    return False


def is_soft_format_title(title: str) -> bool:
    """轻量格式噪（马上评/数说类），不当栏目删，仅软降权。"""
    t = title or ""
    return bool(t) and bool(SOFT_FORMAT_TITLE_RE.search(t))


def is_soft_topic_title(title: str) -> bool:
    """纯体育/娱乐高热：有软话题特征且无硬新闻锚点。"""
    t = title or ""
    if not t or not SOFT_TOPIC_RE.search(t):
        return False
    if HARD_NEWS_ANCHOR_RE.search(t):
        return False
    return True


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_ranks(meta: Any) -> List[int]:
    if not isinstance(meta, dict):
        return []
    ranks = meta.get("ranks") or []
    out: List[int] = []
    for r in ranks:
        try:
            out.append(int(r))
        except (TypeError, ValueError):
            continue
    return out


def score_cluster(
    ranks: Sequence[int],
    count: int,
    date_span: int,
    platform_count: int,
    source_type: str,
    title: str = "",
) -> float:
    """核心三维（可参考 config advanced.weight）+ 周尺度跨天/跨平台；栏目/软话题/软格式降权。"""
    span_bonus = min(max(date_span, 1), 7) * 3.0
    platform_bonus = min(max(platform_count, 1), 5) * 4.0
    # 单平台跨很多天：多半是栏目连载，削弱跨天红利
    if platform_count <= 1 and date_span >= 3:
        span_bonus *= 0.25
    freq_part = min(max(count, 1), 10) * 10.0

    if source_type == "rss" or not ranks:
        score = RSS_WEIGHT_FREQUENCY * freq_part + RSS_WEIGHT_SPAN * span_bonus
    else:
        rank_part = (sum(11 - min(r, 10) for r in ranks) / len(ranks)) * 10.0
        hot_part = (sum(1 for r in ranks if r <= 5) / len(ranks)) * 100.0
        # 三维按 yaml 相对比例，再缩放到 CORE_WEIGHT_SHARE；剩余给 span/platform
        core = (
            WEIGHT_RANK * rank_part
            + WEIGHT_FREQUENCY * freq_part
            + WEIGHT_HOTNESS * hot_part
        )
        score = (
            CORE_WEIGHT_SHARE * core
            + WEIGHT_SPAN * span_bonus
            + WEIGHT_PLATFORM * platform_bonus
        )

    if is_column_title(title):
        # 模板帖允许进池垫底，但不该占 Top
        score *= COLUMN_PENALTY
    else:
        # 纯体育/娱乐高热（如「詹姆斯加盟76人」）轻降权，不删除
        if is_soft_topic_title(title):
            score *= SOFT_TOPIC_PENALTY
        # 马上评/数说等格式噪，轻于栏目
        if is_soft_format_title(title):
            score *= SOFT_FORMAT_PENALTY
    return score


def _pick_better_title(current: str, candidate: str) -> str:
    if not current:
        return candidate
    if not candidate:
        return current
    # 信息更全：更长且不是纯装饰扩展
    if len(candidate) > len(current) + 2:
        return candidate
    return current


def _merge_exact_item(bucket: Dict[str, Dict[str, Any]], item: Dict[str, Any]) -> None:
    key = item["merge_key"]
    if key not in bucket:
        bucket[key] = {
            "title": item["title"],
            "merge_key": key,
            "source_type": item["source_type"],
            "platforms": set(item["platforms"]),
            "dates": set(item["dates"]),
            "ranks": list(item["ranks"]),
            "count": int(item["count"]),
            "member_titles": [item["title"]],
            "is_column": bool(item.get("is_column")),
        }
        return

    existing = bucket[key]
    # 栏目帖优先保留更短/更稳的模板标题；事件帖优先信息更全
    if existing.get("is_column") or item.get("is_column"):
        if len(item["title"]) < len(existing["title"]):
            existing["title"] = item["title"]
    else:
        existing["title"] = _pick_better_title(existing["title"], item["title"])
    existing["platforms"].update(item["platforms"])
    existing["dates"].update(item["dates"])
    existing["ranks"].extend(item["ranks"])
    existing["count"] += int(item["count"])
    existing["is_column"] = bool(existing.get("is_column") or item.get("is_column"))
    if item["title"] not in existing["member_titles"]:
        existing["member_titles"].append(item["title"])
    # news 优先于 rss
    if existing["source_type"] != "news" and item["source_type"] == "news":
        existing["source_type"] = "news"
    elif existing["source_type"] != item["source_type"]:
        existing["source_type"] = "mixed"


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    # 去日期后再比，避免「每日汇总(7/28)」与「每日汇总(7/29)」漏并
    aa = strip_title_dates(a)
    bb = strip_title_dates(b)
    return SequenceMatcher(None, aa, bb).ratio()


def aggregate_similar_items(items: List[Dict[str, Any]], threshold: float) -> List[Dict[str, Any]]:
    """上游风格：按权重降序，Jaccard 粗筛 + SequenceMatcher 精算，合并为事件簇。"""
    if not items:
        return []

    prepared = []
    for item in items:
        title = item["title"]
        char_set = set(title)
        prepared.append({"data": item, "char_set": char_set, "set_len": len(char_set)})

    prepared.sort(key=lambda x: x["data"].get("score", 0), reverse=True)
    used = set()
    clusters: List[Dict[str, Any]] = []
    pre_filter = threshold * 0.5

    for i, item in enumerate(prepared):
        if i in used:
            continue
        base = item["data"]
        base_set = item["char_set"]
        base_len = item["set_len"]

        platforms = set(base.get("platforms") or [])
        dates = set(base.get("dates") or [])
        ranks = list(base.get("ranks") or [])
        total_count = int(base.get("count") or 1)
        member_titles = list(base.get("member_titles") or [base["title"]])
        agg_score = float(base.get("score") or 0)
        source_type = base.get("source_type") or "news"
        used.add(i)

        for j in range(i + 1, len(prepared)):
            if j in used:
                continue
            other_prep = prepared[j]
            other = other_prep["data"]
            other_set = other_prep["char_set"]
            other_len = other_prep["set_len"]
            if base_len == 0 or other_len == 0:
                continue
            if min(base_len, other_len) / max(base_len, other_len) < pre_filter:
                continue
            inter = len(base_set & other_set)
            union = len(base_set | other_set)
            jaccard = inter / union if union else 0.0
            if jaccard < pre_filter:
                continue
            if _title_similarity(base["title"], other["title"]) < threshold:
                continue

            platforms.update(other.get("platforms") or [])
            dates.update(other.get("dates") or [])
            ranks.extend(other.get("ranks") or [])
            total_count += int(other.get("count") or 1)
            for t in other.get("member_titles") or [other["title"]]:
                if t not in member_titles:
                    member_titles.append(t)
            # 额外并入权重衰减，避免简单加和爆分
            agg_score += float(other.get("score") or 0) * 0.5
            if source_type != other.get("source_type"):
                source_type = "mixed"
            used.add(j)

        date_list = sorted(dates)
        date_span = 1
        if len(date_list) >= 2:
            try:
                d0 = datetime.strptime(date_list[0], "%Y-%m-%d")
                d1 = datetime.strptime(date_list[-1], "%Y-%m-%d")
                date_span = (d1 - d0).days + 1
            except ValueError:
                date_span = len(date_list)

        best_rank = min(ranks) if ranks else None
        rank_hi = max(ranks) if ranks else None
        is_column = bool(base.get("is_column")) or any(is_column_title(t) for t in member_titles[:5])
        # 用合并后的结构重算一次更稳的分数；栏目帖不取 max(agg) 以免累加回弹
        recomputed = score_cluster(
            ranks,
            total_count,
            date_span,
            len(platforms),
            source_type if source_type != "mixed" else "news",
            title=base.get("title") or "",
        )
        final_score = recomputed if is_column else max(agg_score, recomputed)

        clusters.append(
            {
                "title": base["title"],
                "platforms": sorted(platforms),
                "dates": date_list,
                "date_span": date_span,
                "count": total_count,
                "ranks": ranks,
                "best_rank": best_rank,
                "rank_hi": rank_hi,
                "score": final_score,
                "source_type": source_type,
                "member_titles": member_titles[:8],
                "is_column": is_column,
            }
        )

    clusters.sort(key=lambda x: (-x["score"], x["dates"][0] if x["dates"] else "", x["title"]))
    return clusters


def _ingest_day(
    reader: HistoryReader,
    current: datetime,
    db_type: str,
    platform_counter: Dict[str, int],
    exact_bucket: Dict[str, Dict[str, Any]],
    stats: Dict[str, int],
) -> None:
    try:
        all_titles, id_to_name, _timestamps = reader.read_all_titles_for_date(date=current, db_type=db_type)
    except FileNotFoundError:
        # 缺库/空库：记 missing，供 RSS 覆盖率缩放
        stats[f"missing_{db_type}_days"] = stats.get(f"missing_{db_type}_days", 0) + 1
        return
    except Exception as exc:
        # 真错误不伪装成缺天
        day = current.strftime("%Y-%m-%d")
        print(f"[collect] 读取 {db_type} {day} 失败: {exc}")
        raise

    date_str = current.strftime("%Y-%m-%d")
    for platform_id, titles in all_titles.items():
        platform_name = id_to_name.get(platform_id, platform_id)
        platform_counter.setdefault(platform_name, 0)
        for title, meta in titles.items():
            norm = normalize_title(title)
            if not norm:
                continue
            platform_counter[platform_name] += 1
            stats[f"raw_{db_type}"] = stats.get(f"raw_{db_type}", 0) + 1
            ranks = _parse_ranks(meta)
            count = _safe_int(meta.get("count", 1) if isinstance(meta, dict) else 1, 1)
            column = is_column_title(norm)
            # 栏目帖去日期后合并；事件帖保留原规范化标题
            merge_key = strip_title_dates(norm) if column else norm
            item = {
                "title": norm,
                "merge_key": merge_key or norm,
                "source_type": "news" if db_type == "news" else "rss",
                "platforms": {platform_name},
                "dates": {date_str},
                "ranks": ranks,
                "count": count,
                "is_column": column,
            }
            _merge_exact_item(exact_bucket, item)


def _finalize_exact_items(bucket: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for data in bucket.values():
        platforms = data["platforms"] if isinstance(data["platforms"], set) else set(data["platforms"])
        dates = data["dates"] if isinstance(data["dates"], set) else set(data["dates"])
        date_list = sorted(dates)
        date_span = 1
        if len(date_list) >= 2:
            try:
                d0 = datetime.strptime(date_list[0], "%Y-%m-%d")
                d1 = datetime.strptime(date_list[-1], "%Y-%m-%d")
                date_span = (d1 - d0).days + 1
            except ValueError:
                date_span = len(date_list)
        ranks = list(data.get("ranks") or [])
        source_type = data.get("source_type") or "news"
        count = int(data.get("count") or 1)
        is_column = bool(data.get("is_column")) or is_column_title(data.get("title") or "")
        score = score_cluster(
            ranks,
            count,
            date_span,
            len(platforms),
            "news" if source_type == "mixed" else source_type,
            title=data.get("title") or "",
        )
        items.append(
            {
                "title": data["title"],
                "source_type": source_type,
                "platforms": platforms,
                "dates": dates,
                "ranks": ranks,
                "count": count,
                "member_titles": list(data.get("member_titles") or [data["title"]]),
                "score": score,
                "is_column": is_column,
            }
        )
    items.sort(key=lambda x: (-x["score"], x["title"]))
    return items


def select_with_quota(
    news_clusters: List[Dict[str, Any]],
    rss_clusters: List[Dict[str, Any]],
    max_news: int,
    rss_day_coverage: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """热榜/RSS 动态配额：RSS 质量优先，目标 15%、上限 20%，不合格不补满。"""
    rss_target = int(max_news * RSS_TARGET_RATIO)
    rss_cap = int(max_news * RSS_MAX_RATIO)

    # 缺库时按可用天数缩 RSS 上限。
    if RSS_SCALE_BY_AVAILABLE_DAYS and rss_day_coverage is not None:
        coverage = max(0.0, min(1.0, float(rss_day_coverage)))
        rss_target = int(round(rss_target * coverage))
        rss_cap = int(round(rss_cap * coverage))

    # 只与同一候选窗口内的头部热榜核验，避免对数千簇做无意义的全量两两比较。
    evidence_news = news_clusters[:max_news]
    scored_rss = [(rss_quality(x, evidence_news), x) for x in rss_clusters]
    premium_rss = [x for quality, x in scored_rss if quality >= 2]
    regular_rss = [x for quality, x in scored_rss if quality == 1]
    selected_rss = premium_rss[:rss_cap]
    if len(selected_rss) < rss_target:
        selected_rss.extend(regular_rss[: rss_target - len(selected_rss)])

    news_take = min(max_news - len(selected_rss), len(news_clusters))
    selected = list(news_clusters[:news_take]) + selected_rss
    if len(selected) < max_news:
        selected.extend(news_clusters[news_take : news_take + (max_news - len(selected))])

    selected.sort(
        key=lambda x: (
            -float(x.get("score") or 0),
            x["dates"][0] if x.get("dates") else "",
            x.get("title") or "",
        )
    )
    return selected[:max_news]


def rss_quality(item: Dict[str, Any], news_clusters: Sequence[Dict[str, Any]]) -> int:
    """返回 0=淘汰、1=合格、2=强交叉印证；强证据才可占用 15% 以上额度。"""
    title = (item.get("title") or "").strip()
    if not title or RSS_NOISE_RE.search(title):
        return 0

    platforms = item.get("platforms") or []
    if len(platforms) >= 2:
        return 2

    for news in news_clusters:
        news_title = news.get("title") or ""
        title_chars = set(title)
        news_chars = set(news_title)
        union = len(title_chars | news_chars)
        if not union or len(title_chars & news_chars) / union < RSS_NEWS_MATCH_THRESHOLD * 0.5:
            continue
        if _title_similarity(title, news_title) >= RSS_NEWS_MATCH_THRESHOLD:
            return 2
    return 1 if RSS_HARD_NEWS_RE.search(title) else 0


def _is_hotlist_item(item: Dict[str, Any]) -> bool:
    """邮件头/实体热词只吃热榜（news/mixed），不含纯 RSS。"""
    return (item.get("source_type") or "news") in ("news", "mixed")

def collect_news(
    start_date: datetime,
    end_date: datetime,
    max_news: int = MAX_NEWS,
    sim_threshold: float = SIM_THRESHOLD,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], Dict[str, Any]]:
    """采集 → 精确合并 → 分池相似聚合 → 配额截断。"""
    reader = HistoryReader(PROJECT_ROOT)
    platform_counter: Dict[str, int] = {}
    news_exact: Dict[str, Dict[str, Any]] = {}
    rss_exact: Dict[str, Dict[str, Any]] = {}
    stats: Dict[str, Any] = {
        "raw_news": 0,
        "raw_rss": 0,
        "missing_news_days": 0,
        "missing_rss_days": 0,
        "max_news": max_news,
        "sim_threshold": sim_threshold,
    }

    current = start_date
    while current <= end_date:
        _ingest_day(reader, current, "news", platform_counter, news_exact, stats)
        _ingest_day(reader, current, "rss", platform_counter, rss_exact, stats)
        current += timedelta(days=1)

    news_merged = _finalize_exact_items(news_exact)
    rss_merged = _finalize_exact_items(rss_exact)
    stats["exact_news"] = len(news_merged)
    stats["exact_rss"] = len(rss_merged)
    stats["exact_total"] = len(news_merged) + len(rss_merged)

    news_clusters = aggregate_similar_items(news_merged, sim_threshold)
    rss_clusters = aggregate_similar_items(rss_merged, sim_threshold)
    stats["cluster_news"] = len(news_clusters)
    stats["cluster_rss"] = len(rss_clusters)
    stats["cluster_total"] = len(news_clusters) + len(rss_clusters)

    total_days = max((end_date - start_date).days + 1, 1)
    available_rss_days = max(total_days - int(stats.get("missing_rss_days") or 0), 0)
    rss_day_coverage = available_rss_days / total_days
    stats["total_days"] = total_days
    stats["available_rss_days"] = available_rss_days
    stats["rss_day_coverage"] = round(rss_day_coverage, 4)
    stats["weight_rank"] = round(WEIGHT_RANK, 4)
    stats["weight_frequency"] = round(WEIGHT_FREQUENCY, 4)
    stats["weight_hotness"] = round(WEIGHT_HOTNESS, 4)
    stats["core_weight_share"] = CORE_WEIGHT_SHARE

    selected = select_with_quota(
        news_clusters,
        rss_clusters,
        max_news,
        rss_day_coverage=rss_day_coverage if RSS_SCALE_BY_AVAILABLE_DAYS else None,
    )
    stats["selected"] = len(selected)
    stats["selected_news"] = sum(1 for x in selected if x.get("source_type") in ("news", "mixed"))
    stats["selected_rss"] = sum(1 for x in selected if x.get("source_type") == "rss")
    stats["selected_column"] = sum(1 for x in selected if x.get("is_column"))
    stats["selected_soft_topic"] = sum(1 for x in selected if is_soft_topic_title(x.get("title") or ""))
    stats["selected_soft_format"] = sum(1 for x in selected if is_soft_format_title(x.get("title") or ""))

    sorted_platforms = dict(sorted(platform_counter.items(), key=lambda kv: (-kv[1], kv[0])))

    print(
        "[collect] "
        f"raw_news={stats['raw_news']} raw_rss={stats['raw_rss']} | "
        f"exact={stats['exact_total']} (n={stats['exact_news']},r={stats['exact_rss']}) | "
        f"cluster={stats['cluster_total']} (n={stats['cluster_news']},r={stats['cluster_rss']}) | "
        f"selected={stats['selected']} (n={stats['selected_news']},r={stats['selected_rss']},"
        f"col={stats['selected_column']},soft={stats['selected_soft_topic']},fmt={stats['selected_soft_format']}) | "
        f"rss_cov={stats['rss_day_coverage']} w={stats['weight_rank']}/"
        f"{stats['weight_frequency']}/{stats['weight_hotness']}"
    )
    return selected, sorted_platforms, stats


def _format_cluster_line(idx: int, item: Dict[str, Any], prefix: str = "") -> str:
    dates = item.get("dates") or []
    if len(dates) >= 2:
        date_part = f"{dates[0][5:]}~{dates[-1][5:]}" if len(dates[0]) >= 10 else f"{dates[0]}~{dates[-1]}"
    elif dates:
        date_part = dates[0][5:] if len(dates[0]) >= 10 else dates[0]
    else:
        date_part = "-"

    platforms = item.get("platforms") or []
    if len(platforms) > 3:
        plat_part = "/".join(platforms[:3]) + f"等{len(platforms)}台"
    else:
        plat_part = "/".join(platforms) if platforms else "-"

    best = item.get("best_rank")
    hi = item.get("rank_hi")
    if best is not None and hi is not None and best != hi:
        rank_part = f" | 排名:{best}-{hi}"
    elif best is not None:
        rank_part = f" | 排名:{best}"
    else:
        rank_part = ""

    span = item.get("date_span") or 1
    span_part = f" | 跨天:{span}" if span > 1 else ""
    return (
        f"{prefix}{idx}. [{date_part}] [{plat_part}] {item.get('title', '')} "
        f"| 分:{float(item.get('score') or 0):.1f} | 次:{item.get('count') or 1}{rank_part}{span_part}"
    )


def build_evidence_index(news_items: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    evidence: Dict[str, Dict[str, Any]] = {}
    n_i = r_i = 0
    for item in news_items:
        if item.get("source_type") == "rss":
            r_i += 1
            evidence[f"R{r_i}"] = item
        else:
            n_i += 1
            evidence[f"N{n_i}"] = item
    return evidence


def build_prompt(
    start_date: str,
    end_date: str,
    news_items: List[Dict[str, Any]],
    platform_counter: Dict[str, int],
    pipeline_stats: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """填充 weekly_ai_prompt.txt。"""
    top_platforms = list(platform_counter.items())[:12]
    pipeline_stats = pipeline_stats or {}

    news_lines: List[str] = []
    rss_lines: List[str] = []
    n_i = r_i = 0
    for item in news_items:
        if item.get("source_type") == "rss":
            r_i += 1
            rss_lines.append(_format_cluster_line(r_i, item, prefix="R"))
        else:
            n_i += 1
            news_lines.append(_format_cluster_line(n_i, item, prefix="N"))

    stats_block = {
        "raw_news": pipeline_stats.get("raw_news"),
        "raw_rss": pipeline_stats.get("raw_rss"),
        "exact_total": pipeline_stats.get("exact_total"),
        "cluster_total": pipeline_stats.get("cluster_total"),
        "selected": pipeline_stats.get("selected", len(news_items)),
        "selected_news": pipeline_stats.get("selected_news"),
        "selected_rss": pipeline_stats.get("selected_rss"),
        "rss_day_coverage": pipeline_stats.get("rss_day_coverage"),
    }

    system_content, user_template = _load_prompt_template(WEEKLY_AI_PROMPT_FILE)
    user_content = _fill_prompt_template(
        user_template,
        {
            "start_date": start_date,
            "end_date": end_date,
            "stats_json": json.dumps(stats_block, ensure_ascii=False),
            "news_count": str(len(news_items)),
            "platforms_json": json.dumps(top_platforms, ensure_ascii=False),
            "news_content": "\n".join(news_lines) if news_lines else "（无）",
            "rss_content": "\n".join(rss_lines) if rss_lines else "（无）",
        },
    )
    messages: List[Dict[str, str]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})
    return messages


def _headline_len_ok(label: str) -> bool:
    if label in HEADLINE_LEN_EXCEPTIONS:
        return True
    # 纯英文缩写放宽到 5
    if re.fullmatch(r"[A-Za-z0-9]{2,5}", label):
        return True
    return HEADLINE_MIN_LEN <= len(label) <= HEADLINE_MAX_LEN


def _is_banned_headline(label: str) -> bool:
    if label in HEADLINE_BANNED:
        return True
    # 保留“宇树科技/量子科技”类四字实体，过滤“科技发/树科技”等滑窗碎片。
    if "科技" in label and not (len(label) == 4 and label.endswith("科技")):
        return True
    for bad in ("局势", "政策", "市场", "基建", "热点", "新闻", "消息", "动态"):
        if label.endswith(bad) and len(label) <= 6:
            return True
    return False


def _normalize_headline_token(raw: str) -> str:
    p = re.sub(r"^[0-9一二三四五六七八九十]+[.、]\s*", "", (raw or "").strip())
    p = p.strip(' \n\t-—,，;；.。[]【】"\'“”')
    p = re.sub(r"\s+", "", p)
    return p


# 灾种/事件后缀：与标题中地名拼成「日本地震」类合成词（中间可夹其它字）
_EVENT_COMPOUND_SUFFIXES = (
    "地震", "台风", "暴雨", "山火", "海啸", "洪灾", "山洪", "泥石流", "崩塌",
)
_PLACE_HINT_RE = re.compile(
    r"(日本|中国|美国|伊朗|以色列|台湾|香港|澳门|新疆|西藏|青海|四川|云南|甘肃|"
    r"陕西|山西|河北|河南|山东|江苏|浙江|福建|广东|广西|湖南|湖北|安徽|江西|"
    r"辽宁|吉林|黑龙江|贵州|海南|重庆|北京|上海|天津|宁夏|内蒙古|"
    r"熊本|东京|大阪|北海道|台湾新北|新北|花莲|宜兰)"
)


def _iter_event_compounds(title: str) -> List[str]:
    """从标题合成事件切口，避免规则回退拆成「日本」「地震」。"""
    title = title or ""
    out: List[str] = []
    for sfx in _EVENT_COMPOUND_SUFFIXES:
        if sfx not in title:
            continue
        for m in _PLACE_HINT_RE.finditer(title):
            place = m.group(1)
            # 地名应出现在灾种之前（或同句），且合成后 3~4 字优先
            if m.start() > title.find(sfx):
                continue
            compound = f"{place}{sfx}"
            # 「台湾新北地震」过长则收成「台湾地震」
            if len(compound) > HEADLINE_MAX_LEN:
                if place.startswith("台湾") and len(f"台湾{sfx}") <= HEADLINE_MAX_LEN:
                    compound = f"台湾{sfx}"
                elif len(place) >= 2 and len(f"{place[:2]}{sfx}") <= HEADLINE_MAX_LEN:
                    compound = f"{place[:2]}{sfx}"
                else:
                    continue
            if HEADLINE_MIN_LEN <= len(compound) <= HEADLINE_MAX_LEN:
                out.append(compound)
        # 无地名命中但标题含「X级地震」等，保留灾种本身由 n-gram 处理
    return out


def _iter_short_title_tokens(title: str) -> List[str]:
    """标题短实体：英文专名 + 中文 2/3/4 元 + 事件合成词。"""
    title = title or ""
    out: List[str] = []
    for eng in re.findall(r"[A-Za-z][A-Za-z0-9]{1,7}", title):
        out.append(eng)
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", title):
        if 2 <= len(seg) <= 4:
            out.append(seg)
        for n in (2, 3, 4):
            if len(seg) < n:
                continue
            for i in range(0, len(seg) - n + 1):
                out.append(seg[i : i + n])
    out.extend(_iter_event_compounds(title))
    return out


def _merge_split_event_headlines(selected: List[str], titles: Sequence[str]) -> List[str]:
    """若已选「日本」「地震」且标题可支撑「日本地震」，合并为合成词。"""
    if not selected:
        return selected
    title_blob = "\n".join(titles or [])
    merged = list(selected)
    for sfx in _EVENT_COMPOUND_SUFFIXES:
        if sfx not in merged:
            continue
        places = [x for x in merged if x != sfx and x in title_blob and sfx in title_blob]
        # 仅当地名与灾种同现于至少一条标题
        for place in places:
            compound = f"{place}{sfx}"
            if len(compound) > HEADLINE_MAX_LEN:
                continue
            if not any(place in t and sfx in t for t in titles):
                continue
            # 用合成词替换 place+sfx（保留相对靠前位置）
            idx = min(merged.index(place), merged.index(sfx))
            merged = [x for x in merged if x not in (place, sfx)]
            if compound not in merged:
                merged.insert(idx, compound)
            break
    return merged


_RULE_NOISE = {
    "措施", "路径", "影响", "预期", "相关", "问题", "情况", "方面", "工作", "进行",
    "表示", "指出", "回应", "报道", "最新", "今日", "本周", "创新", "大涨", "升温",
    "推动", "解读", "官员", "同向", "波动", "再创", "新高", "引发", "关注", "阶段",
    "市场", "军方", "股价", "发布", "芯片", "同向", "押注", "谈及", "谈降",
    "规模", "科技", "早盘", "盘收", "上涨", "发行", "最高", "年期", "利率", "倍数", "登陆",
}

_RULE_FUNCTION_SUFFIXES = {
    "措施", "路径", "影响", "预期", "问题", "情况", "方面", "工作", "波动",
    "新高", "阶段", "市场", "股价", "利率", "倍数", "规模", "发行", "上涨", "最高", "年期",
}


def _is_redundant_headline(token: str, selected: Sequence[str]) -> bool:
    for prev in selected:
        if token == prev:
            return True
        # 互相包含：保留已选更长词，跳过更短碎片
        if token in prev or prev in token:
            return True
        if len(token) >= 2 and len(prev) >= 2:
            inter = len(set(token) & set(prev))
            if inter / max(len(set(token) | set(prev)), 1) >= 0.7:
                return True
    return False


def build_rule_entity_headlines(news_items: List[Dict[str, Any]], topn: int = 5) -> List[str]:
    """AI 失败时的规则兜底：仅热榜标题，多标题共现短实体。"""
    scores: Counter = Counter()
    df: Counter = Counter()
    hotlist = [x for x in news_items if _is_hotlist_item(x)]
    for item in hotlist[:200]:
        title = item.get("title") or ""
        if item.get("is_column") or is_column_title(title) or RULE_DATA_TEMPLATE_TITLE_RE.search(title):
            continue
        weight = max(float(item.get("score") or 1.0), 1.0)
        seen_in_title = set()
        for token in _iter_short_title_tokens(title):
            token = _normalize_headline_token(token)
            if not token or _is_banned_headline(token):
                continue
            if token in STOPWORDS or token in NOISE_WORDS or token in _RULE_NOISE:
                continue
            # 以功能后缀结尾的 3–4 字词（如「关税措施」）降级剔除；
            # 不复用 _RULE_NOISE，避免误伤“宇树科技/存储芯片”等实体。
            if len(token) >= 3 and any(token.endswith(sfx) for sfx in _RULE_FUNCTION_SUFFIXES):
                continue
            if re.search(r"亿|万|美元|元|%", token):
                continue
            if not _headline_len_ok(token):
                continue
            if token.isascii():
                bonus = 1.5
            else:
                # 更长 n-gram 略加分，便于「英伟达」「以色列」压过「英伟」
                bonus = 0.75 + 0.25 * len(token)
            scores[token] += weight * bonus
            if token not in seen_in_title:
                df[token] += 1
                seen_in_title.add(token)

    ranked = sorted(
        (
            (token, sc * (0.15 + df[token] ** 1.6) * (1.05 if len(token) >= 3 or token.isascii() else 1.0))
            for token, sc in scores.items()
        ),
        key=lambda x: (-x[1], -len(x[0]), x[0]),
    )

    selected: List[str] = []
    for token, _ in ranked:
        # 宁可少于 5 个，也不使用只在单条标题中出现的滑窗碎片。
        if df[token] < 2:
            continue
        if _is_redundant_headline(token, selected):
            continue
        selected.append(token)
        if len(selected) >= topn:
            return selected
    return selected


def build_keyword_prompt(
    start_date: str,
    end_date: str,
    report_text: str,
) -> List[Dict[str, str]]:
    """填充 weekly_keyword_prompt.txt。"""
    system_content, user_template = _load_prompt_template(WEEKLY_KEYWORD_PROMPT_FILE)
    user_content = _fill_prompt_template(
        user_template,
        {
            "start_date": start_date,
            "end_date": end_date,
            "report_text": report_text,
        },
    )
    messages: List[Dict[str, str]] = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.append({"role": "user", "content": user_content})
    return messages


def _parse_keyword_list(raw: str) -> List[str]:
    text = (raw or "").strip()
    if not text:
        return []
    # 去掉可能的代码围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    # 尝试截取首个 [...]
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
    return [p.strip() for p in re.split(r"[/／|｜,，;；\n]", text) if p.strip()]


THEMES_BLOCK_RE = re.compile(
    r"<THEMES_JSON>\s*([\s\S]*?)\s*</THEMES_JSON>\s*",
    re.I,
)
REPORT_BLOCK_RE = re.compile(
    r"<REPORT_MARKDOWN>\s*([\s\S]*?)\s*</REPORT_MARKDOWN>",
    re.I,
)


def parse_structured_report(
    raw: str,
    evidence_index: Dict[str, Dict[str, Any]],
) -> Tuple[str, List[Dict[str, Any]]]:
    """解析主题证据块；主题无效时仍保留正文，供独立关键词 Prompt 回退。"""
    text = (raw or "").strip()
    report_match = REPORT_BLOCK_RE.search(text)
    if report_match:
        report_text = report_match.group(1).strip()
    else:
        report_text = THEMES_BLOCK_RE.sub("", text).strip()
        report_text = re.sub(r"^\s*<REPORT_MARKDOWN>\s*", "", report_text, flags=re.I).strip()

    themes_match = THEMES_BLOCK_RE.search(text)
    if not themes_match:
        return report_text, []
    try:
        payload = json.loads(themes_match.group(1))
    except Exception:
        return report_text, []
    if not isinstance(payload, list):
        return report_text, []

    valid_themes: List[Dict[str, Any]] = []
    for raw_theme in payload[:8]:
        if not isinstance(raw_theme, dict):
            continue
        title = str(raw_theme.get("title") or "").strip()
        keyword = _normalize_headline_token(str(raw_theme.get("keyword") or ""))
        raw_ids = raw_theme.get("evidence_ids") or []
        evidence_ids = []
        for evidence_id in raw_ids if isinstance(raw_ids, list) else []:
            normalized_id = str(evidence_id).strip().upper()
            if normalized_id in evidence_index and normalized_id not in evidence_ids:
                evidence_ids.append(normalized_id)
        if not title or not keyword or _is_banned_headline(keyword) or not _headline_len_ok(keyword):
            continue
        if keyword not in report_text or len(evidence_ids) < 2:
            continue
        valid_themes.append(
            {"title": title, "keyword": keyword, "evidence_ids": evidence_ids[:5]}
        )
    return report_text, valid_themes


def keywords_from_themes(themes: Sequence[Dict[str, Any]], topn: int = KEYWORD_TOPN) -> List[str]:
    return _filter_headlines([str(theme.get("keyword") or "") for theme in themes])[:topn]


def _filter_headlines(
    candidates: Sequence[str],
    report_text: str = "",
) -> List[str]:
    cleaned: List[str] = []
    for p in candidates:
        if not isinstance(p, str):
            continue
        p = _normalize_headline_token(p)
        if not p or _is_banned_headline(p):
            continue
        if report_text and p not in report_text:
            continue
        if not _headline_len_ok(p):
            if re.fullmatch(r"[\u4e00-\u9fff]{5,8}", p):
                p = p[:4]
                if _is_banned_headline(p) or not _headline_len_ok(p):
                    continue
            else:
                continue
        if p not in cleaned:
            cleaned.append(p)
        if len(cleaned) >= KEYWORD_TOPN:
            break
    return cleaned


def extract_headline_keywords(
    client: AIClient,
    start_date: str,
    end_date: str,
    news_items: List[Dict[str, Any]],
    report_text: str,
    title_limit: int = KEYWORD_TITLE_LIMIT,
    topn: int = KEYWORD_TOPN,
) -> Tuple[List[str], str]:
    """从周报正文提炼邮件头关键词，并用热榜标题核验支撑。"""
    titles: List[str] = []
    for item in news_items:
        if not _is_hotlist_item(item):
            continue
        if item.get("is_column") or is_column_title(item.get("title") or ""):
            continue
        t = (item.get("title") or "").strip()
        if t and t not in titles:
            titles.append(t)
        if len(titles) >= title_limit:
            break

    rule_fallback = _filter_headlines(
        build_rule_entity_headlines(news_items, topn=max(topn * 4, 20)),
        report_text,
    )
    rule_fallback = _filter_headlines(
        _merge_split_event_headlines(rule_fallback, titles),
        report_text,
    )
    if not titles:
        return rule_fallback[:topn], "rule_only_empty_titles"

    keyword_client = build_keyword_client(
        {
            "MODEL": client.model,
            "API_KEY": client.api_key,
            "API_BASE": client.api_base,
            "TIMEOUT": client.timeout,
            "FALLBACK_MODELS": list(client.fallback_models),
        }
    )
    messages = build_keyword_prompt(start_date, end_date, report_text)
    last_error = ""
    for attempt in range(2):
        try:
            raw = keyword_client.chat(messages)
            parsed = _parse_keyword_list(raw)
            cleaned = _filter_headlines(parsed, report_text)
            cleaned = _filter_headlines(
                _merge_split_event_headlines(cleaned, titles),
                report_text,
            )
            for p in rule_fallback:
                if p not in cleaned:
                    cleaned.append(p)
                if len(cleaned) >= topn:
                    break
            if len(cleaned) >= 3:
                cleaned = _filter_headlines(
                    _merge_split_event_headlines(cleaned[:topn], titles),
                    report_text,
                )
                src = "ai_lite" if attempt == 0 else "ai_lite_retry"
                return cleaned[:topn], src
            preview = re.sub(r"\s+", " ", (raw or "").strip())[:80]
            last_error = f"valid_labels={len(cleaned)} raw={preview!r}"
            print(f"[关键词] lite 无效输出 attempt={attempt + 1}: {last_error}")
        except Exception as e:
            last_error = str(e)
            print(f"[关键词] lite 抽取失败 attempt={attempt + 1}: {e}")

    print(f"[关键词] 回退规则实体 Top{topn}（{last_error}）")
    return rule_fallback[:topn], "rule_only"


def _inline_markdown_to_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"\*(.+?)\*", r"<em>\1</em>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def markdown_to_basic_html(md: str) -> str:
    lines = md.splitlines()
    parts: List[str] = []
    in_ul = False
    in_ol = False
    paragraph: List[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            parts.append(f"<p>{'<br>'.join(_inline_markdown_to_html(x) for x in paragraph)}</p>")
            paragraph = []

    def close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            parts.append("</ul>")
            in_ul = False
        if in_ol:
            parts.append("</ol>")
            in_ol = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_lists()
            continue
        if stripped.startswith("### "):
            flush_paragraph()
            close_lists()
            parts.append(f"<h3>{_inline_markdown_to_html(stripped[4:])}</h3>")
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            close_lists()
            parts.append(f"<h2>{_inline_markdown_to_html(stripped[3:])}</h2>")
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            close_lists()
            parts.append(f"<h1>{_inline_markdown_to_html(stripped[2:])}</h1>")
            continue
        if re.match(r"^\d+\.\s+", stripped):
            flush_paragraph()
            if in_ul:
                parts.append("</ul>")
                in_ul = False
            if not in_ol:
                parts.append("<ol>")
                in_ol = True
            item = re.sub(r"^\d+\.\s+", "", stripped)
            parts.append(f"<li>{_inline_markdown_to_html(item)}</li>")
            continue
        if stripped.startswith(("- ", "*   ", "* ")):
            flush_paragraph()
            if in_ol:
                parts.append("</ol>")
                in_ol = False
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            item = re.sub(r"^(?:- |\*\s+)", "", stripped)
            parts.append(f"<li>{_inline_markdown_to_html(item)}</li>")
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            close_lists()
            parts.append(f"<blockquote>{_inline_markdown_to_html(stripped.lstrip('> ').strip())}</blockquote>")
            continue
        paragraph.append(stripped)

    flush_paragraph()
    close_lists()
    return "\n".join(parts)


def render_html(title: str, date_range: str, model_name: str, statistics: Dict[str, str], report_markdown: str) -> str:
    report_markdown = re.sub(r"^#\s+.*周报.*\n+", "", report_markdown.strip(), count=1, flags=re.MULTILINE)
    # 去掉 markdown 水平线，改由标题块承担分区，避免横线割裂
    report_markdown = re.sub(r"(?m)^(?:---+|\*\*\*+|___+)\s*$", "", report_markdown)
    # 与 Header「收集时间」重复的周期/样本元信息不进正文（模型常写成 统计/分析周期、数据样本）
    report_markdown = re.sub(
        r"(?m)^[（(]?\s*(?:\*\*)?(?:统计周期|分析周期|收集时间|时间范围|本周周期|数据样本)(?:\*\*)?\s*[:：].+$\n?",
        "",
        report_markdown,
        count=3,
    )
    report_markdown = re.sub(
        r"(?m)^[（(]\s*(?:统计周期|分析周期)\s*[:：].+[）)]\s*$\n?",
        "",
        report_markdown,
        count=1,
    )
    report_markdown = report_markdown.lstrip("\n")
    report_html = markdown_to_basic_html(report_markdown)
    # HTML 层再剥首段元信息（模型有时把周期+样本塞进同一段）
    report_html = re.sub(
        r"^\s*<p>(?:(?!</p>).)*(?:统计周期|分析周期|收集时间|时间范围|数据样本)(?:(?!</p>).)*</p>\s*",
        "",
        report_html,
        count=1,
        flags=re.I | re.S,
    )
    keyword_text = statistics.get("Top5关键词") or statistics.get("Top3关键词", "-")
    tab_html = "".join(
        f'<span class="tab-pill">{html.escape(part.strip())}</span>'
        for part in keyword_text.split("/")
        if part.strip()
    )
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 官方 Gemini 模型名直接展示（如 Gemini-3.5-flash），不再剥离 provider/ 前缀
    display_model_name = (model_name or "").strip() or "-"
    display_model_name = display_model_name[:1].upper() + display_model_name[1:]
    # 双卡片 + 网页自适应：默认浅色；prefers-color-scheme: dark 切 Apple Settings 风格深色 token
    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <meta name=\"color-scheme\" content=\"light dark\">
  <meta name=\"supported-color-schemes\" content=\"light dark\">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --page-bg: #ffffff;
      --text: #1c1c1e;
      --quote-bg: #f8fafc;
      --quote-border: #c7d2fe;
      --quote-text: #475467;
      --code-bg: #f3f4f6;
      --header-shadow: 0 8px 24px rgba(49, 134, 255, 0.18);
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{
        --page-bg: #000000;
        --text: #f5f5f7;
        --quote-bg: #2c2c2e;
        --quote-border: #6366f1;
        --quote-text: #ebebf5;
        --code-bg: #2c2c2e;
        --header-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
      }}
    }}
    body {{
      margin: 0;
      padding: 0;
      background: var(--page-bg);
      font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Arial, sans-serif;
      color: var(--text);
      line-height: 1.72;
      -webkit-text-size-adjust: 100%;
    }}
    .page {{
      max-width: 920px;
      margin: 0 auto;
      padding: 0 12px 28px;
    }}
    .container {{
      background: transparent;
      box-shadow: none;
      overflow: visible;
    }}
    .header {{
      background-color: #3186FF;
      background-image: linear-gradient(
        to top right,
        #3186FF 0%,
        #3186FF 75%,
        #A9A8FF 99.6%
      );
      color: #fff;
      padding: 18px 18px 14px;
      position: relative;
      overflow: hidden;
      border-radius: 16px;
      margin: 0 0 8px 0;
      box-shadow: var(--header-shadow);
    }}
    .header-title {{
      position: relative;
      z-index: 1;
      margin: 0 0 6px 0;
      font-size: 22px;
      line-height: 1.25;
      font-weight: 800;
      color: #fff;
    }}
    .header-meta {{
      position: relative;
      z-index: 1;
      font-size: 12px;
      line-height: 1.6;
      color: rgba(255,255,255,0.92);
    }}
    .header-meta-row {{
      display: block;
      margin: 0 0 2px 0;
    }}
    .header-meta strong {{ color: #fff; }}
    .tab-strip {{
      position: relative;
      z-index: 1;
      margin-top: 12px;
      display: block;
      white-space: nowrap;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      padding-bottom: 2px;
    }}
    .tab-strip::-webkit-scrollbar {{ height: 3px; }}
    .tab-pill {{
      display: inline-block;
      margin: 0 6px 6px 0;
      padding: 5px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.18);
      border: 1px solid rgba(255,255,255,0.22);
      color: #fff;
      font-size: 11px;
      font-weight: 600;
      line-height: 1.2;
      white-space: nowrap;
    }}
    .content {{
      margin: 0;
      padding: 0;
      background: transparent;
    }}
    /* AI 卡：外层渐变描边 + 内层白底，避免渐变渗入正文背景；不使用 mask/filter */
    .report-shell {{
      border-radius: 16px;
      padding: 2.5px;
      background: linear-gradient(to bottom right, #0894ff 0%, #c959dd 34%, #ff2e54 68%, #ff9004);
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }}
    .report {{
      position: relative;
      overflow: hidden;
      background: #ffffff;
      border: none;
      border-radius: 13.5px;
      padding: 16px 18px 18px;
      color: #374151;
      box-shadow:
        inset 0 0 10px rgba(8, 148, 255, 0.14),
        inset 0 0 14px rgba(201, 89, 221, 0.11),
        inset 0 0 18px rgba(255, 46, 84, 0.09),
        inset 0 0 22px rgba(255, 144, 4, 0.08);
    }}
    .report h1, .report h2, .report h3 {{
      line-height: 1.35;
      margin-top: 1.05em;
      margin-bottom: 0.45em;
      color: #3730a3;
    }}
    .report h1:first-child, .report h2:first-child {{ margin-top: 0; }}
    .report h2 {{
      font-size: 18px;
      line-height: 1.35;
      padding: 0;
      margin: 20px 0 9px;
      background: transparent;
      border: none;
      border-radius: 0;
      color: #3730a3;
      font-weight: 700;
    }}
    .report h3 {{
      font-size: 15px;
      line-height: 1.35;
      color: #3730a3;
      margin: 14px 0 6px;
      padding: 0;
      background: transparent;
      border: none;
      border-radius: 0;
      font-weight: 700;
    }}
    .report p {{
      margin: 0.4em 0;
      font-size: 14px;
      line-height: 1.55;
      color: #334155;
    }}
    .report ul, .report ol {{
      padding-left: 1.25em;
      margin: 6px 0 10px;
    }}
    .report li {{
      margin: 0.18em 0;
      font-size: 14px;
      line-height: 1.55;
      color: #334155;
    }}
    .report strong {{ font-weight: 800; color: #1e293b; }}
    .report blockquote {{
      border-left: 4px solid var(--quote-border);
      margin: 1em 0;
      padding: 0.55em 0.9em;
      color: var(--quote-text);
      background: var(--quote-bg);
      border-radius: 16px;
    }}
    .report code {{
      background: var(--code-bg);
      padding: 0.1em 0.35em;
      border-radius: 4px;
      color: var(--text);
    }}
    @media (prefers-color-scheme: dark) {{
      .report-shell {{
        background: linear-gradient(to bottom right, #0894ff 0%, #c959dd 34%, #ff2e54 68%, #ff9004) !important;
      }}
      .report {{
        background: #1c1c1e !important;
        color: #f5f5f7 !important;
        box-shadow:
          inset 0 0 12px rgba(8, 148, 255, 0.18),
          inset 0 0 16px rgba(201, 89, 221, 0.14),
          inset 0 0 20px rgba(255, 46, 84, 0.12),
          inset 0 0 24px rgba(255, 144, 4, 0.10) !important;
      }}
      .report h1, .report h2, .report h3 {{
        color: #c7d2fe !important;
      }}
      .report p, .report li {{
        color: #ebebf5 !important;
      }}
      .report strong {{
        color: #e0e7ff !important;
      }}
    }}
    @media (max-width: 640px) {{
      .page {{ padding: 0 8px 18px; }}
      .header {{
        border-radius: 16px;
        margin-bottom: 6px;
        padding: 14px 14px 12px;
        box-shadow: var(--header-shadow);
      }}
      .header-title {{ font-size: 17px; margin-bottom: 3px; }}
      .header-meta {{ font-size: 11px; line-height: 1.45; }}
      .tab-strip {{ margin-top: 8px; }}
      .tab-pill {{ padding: 3px 8px; font-size: 10px; margin: 0 4px 4px 0; }}
      .report-shell {{
        border-radius: 16px;
        padding: 2.5px;
      }}
      .report {{
        border-radius: 13.5px;
        padding: 12px 12px 14px;
      }}

      .report h2 {{
        font-size: 17px;
        padding: 0;
        margin: 14px 0 8px;
        border-radius: 0;
      }}
      .report h3 {{ font-size: 14px; }}
      .report p, .report li {{ font-size: 13px; line-height: 1.6; }}
    }}
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"container\">
      <div class=\"header\">
        <div class=\"header-title\">{html.escape(title)}</div>
        <div class=\"header-meta\">
          <div class=\"header-meta-row\"><strong>收集时间</strong>：{html.escape(date_range)}</div>
          <div class=\"header-meta-row\"><strong>周报模型</strong>：{html.escape(display_model_name)}</div>
          <div class=\"header-meta-row\"><strong>生成时间</strong>：{html.escape(generated_at)}</div>
        </div>
        <div class=\"tab-strip\">{tab_html}</div>
      </div>
      <div class=\"content\">
        <div class=\"report-shell\">
          <div class=\"report\">{report_html}</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 AI 版 TrendRadar 周报并通过邮件发送")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--to", help="覆盖收件人，多个用逗号分隔")
    parser.add_argument("--subject", help="覆盖邮件主题")
    parser.add_argument("--model", default="", help="可选：覆盖 AI_MODEL（本地调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只生成报告文件，不发送邮件")
    args = parser.parse_args()

    env = load_runtime_env()

    start_date, end_date = resolve_date_range(args.start, args.end)
    all_news, platform_counter, pipeline_stats = collect_news(
        start_date,
        end_date,
    )
    if not all_news:
        print("未读取到可用于生成周报的数据")
        return 1
    ai_config = load_ai_config(str(PROJECT_ROOT / "config" / "config.yaml"))
    if (args.model or "").strip():
        ai_config["MODEL"] = args.model.strip()
    ai_model = str(ai_config.get("MODEL") or "").strip()
    if not ai_model:
        print("未配置 AI 模型，请设置 AI_MODEL 或 config.yaml 的 ai.model")
        return 2
    client = AIClient(ai_config)
    ok, error = client.validate_config()
    if not ok:
        print(error)
        return 2

    start_s = start_date.strftime("%Y-%m-%d")
    end_s = end_date.strftime("%Y-%m-%d")

    # 主干①：周报正文（空结果/异常时再试 1 次；模型链 fallback 由 AIClient 处理）
    messages = build_prompt(
        start_s,
        end_s,
        all_news,
        platform_counter,
        pipeline_stats,
    )
    raw_report = ""
    last_report_err = ""
    used_model = ""
    for attempt in range(2):
        try:
            raw_report = (client.chat(messages) or "").strip()
            if raw_report:
                used_model = (getattr(client, "last_model", None) or "").strip()
                break
            last_report_err = "empty_response"
            print(f"[周报正文] 空响应 attempt={attempt + 1}")
        except Exception as exc:
            last_report_err = str(exc)
            print(f"[周报正文] 调用失败 attempt={attempt + 1}: {exc}")
    if not raw_report:
        print(f"周报正文生成失败: {last_report_err or 'unknown'}")
        return 5

    evidence_index = build_evidence_index(all_news)
    report_text, themes = parse_structured_report(raw_report, evidence_index)
    headline_keywords = keywords_from_themes(themes)
    if len(headline_keywords) >= KEYWORD_TOPN:
        keyword_source = "structured_themes"
    else:
        # 保留已验证的主题关键词，独立 Prompt 仅补足缺额。
        fallback_keywords, fallback_source = extract_headline_keywords(
            client,
            start_s,
            end_s,
            all_news,
            report_text,
        )
        for keyword in fallback_keywords:
            if keyword not in headline_keywords:
                headline_keywords.append(keyword)
            if len(headline_keywords) >= KEYWORD_TOPN:
                break
        keyword_source = (
            f"structured_themes+{fallback_source}"
            if themes
            else fallback_source
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dr = f"{start_s} ~ {end_s}"
    base_name = f"weekly-ai-{start_s}-to-{end_s}-{stamp}"
    html_path = OUTPUT_DIR / f"{base_name}.html"
    keywords_path = OUTPUT_DIR / f"{base_name}.keywords.json"

    title = "AI 每周新闻分析"
    stats = {
        "去重/送入簇": len(all_news),
        "覆盖平台": len(platform_counter),
        "Top5关键词": " / ".join(headline_keywords) if headline_keywords else "-",
        "关键词来源": keyword_source,
        "raw_news": pipeline_stats.get("raw_news"),
        "raw_rss": pipeline_stats.get("raw_rss"),
        "exact": pipeline_stats.get("exact_total"),
        "cluster": pipeline_stats.get("cluster_total"),
    }
    # Header 展示正文分析实际模型（含 fallback），非仅配置主模型
    html_path.write_text(
        render_html(title, dr, used_model or ai_model, stats, report_text),
        encoding="utf-8",
    )
    keywords_path.write_text(
        json.dumps(
            {
                "date_range": {"start": start_s, "end": end_s},
                "final": headline_keywords,
                "source": keyword_source,
                "themes": themes,
                "rule_fallback": build_rule_entity_headlines(all_news, topn=12),
                "pipeline": pipeline_stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"HTML: {html_path}")
    print(f"Keywords: {keywords_path}")
    print(json.dumps(stats, ensure_ascii=False))

    if args.dry_run:
        print("dry-run: 已跳过邮件发送")
        return 0

    to_email = args.to or env.get("EMAIL_TO", "")
    from_email = env.get("EMAIL_FROM", "")
    password = env.get("EMAIL_PASSWORD", "")
    smtp_server = env.get("EMAIL_SMTP_SERVER", "")
    smtp_port = env.get("EMAIL_SMTP_PORT", "")
    missing = [
        key
        for key, value in {
            "EMAIL_FROM": from_email,
            "EMAIL_PASSWORD": password,
            "EMAIL_TO": to_email,
        }.items()
        if not value
    ]
    if missing:
        print(f"缺少邮件配置: {', '.join(missing)}")
        return 3

    if bool(smtp_server) != bool(smtp_port):
        partial_missing = []
        if not smtp_server:
            partial_missing.append("EMAIL_SMTP_SERVER")
        if not smtp_port:
            partial_missing.append("EMAIL_SMTP_PORT")
        print(f"缺少邮件配置: {', '.join(partial_missing)}")
        return 3

    final_subject = args.subject or f"AI周报（{dr}）"
    ok = send_to_email(
        from_email=from_email,
        password=password,
        to_email=to_email,
        report_type=final_subject,
        html_file_path=str(html_path),
        custom_smtp_server=smtp_server or None,
        custom_smtp_port=int(smtp_port) if smtp_port else None,
        subject_override=final_subject,
        sender_name_override="AI周报",
    )
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
