"""Weekly report data ingestion, scoring, clustering, and selection."""
from __future__ import annotations

from datetime import datetime, timedelta
from difflib import SequenceMatcher
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trendradar.ai.dataflow import is_market_dataflow_title
from trendradar.storage.history_reader import HistoryReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]

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
# 行情/经济数据播报：允许入池垫底，但不得占据叙事分析的 Top 位。
# 实测旧逻辑下跨天 TOP12 全部是单平台行情流（span7d、成员至8）。
DATAFLOW_PENALTY = 0.10

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
    """Optionally sync the core score weights from config.yaml."""
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
        WEIGHT_RANK = rank / total
        WEIGHT_FREQUENCY = freq / total
        WEIGHT_HOTNESS = hot / total
    except Exception as exc:
        print(f"[weight] load config.yaml failed, keep defaults: {exc}")


_load_core_weights_from_config()


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
    platforms: Any = None,
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
    elif is_market_dataflow_title(title, platforms):
        # 行情/数据播报：模板化数字流，不是叙事事件
        score *= DATAFLOW_PENALTY
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
    """上游风格：按权重降序，Jaccard 粗筛 + SequenceMatcher 精算，合并为事件簇。

    行情/经济数据播报不参与跨条目合并：这类标题是「同一模板 + 每日变化的数字」，
    strip_title_dates 之后模板骨架高度重合，而周报侧不做数字加权，已实测把
    「涨5%」与「跌2.3%」、「澳洲CPI」与「德国PPI」合并成 span7d 的假事件簇并顶到榜首。
    """
    if not items:
        return []

    prepared = []
    for item in items:
        title = item["title"]
        char_set = set(title)
        prepared.append({
            "data": item,
            "char_set": char_set,
            "set_len": len(char_set),
            "is_dataflow": is_market_dataflow_title(title, item.get("platforms")),
        })

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
        base_is_dataflow = item["is_dataflow"]
        used.add(i)

        # 行情播报自成一簇，不向外吸收成员
        inner = [] if base_is_dataflow else range(i + 1, len(prepared))
        for j in inner:
            if j in used:
                continue
            other_prep = prepared[j]
            if other_prep["is_dataflow"]:
                continue
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
            platforms=platforms,
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
        # 真错误不伪装成缺天；异常原文可能包含路径、URL 或凭据。
        day = current.strftime("%Y-%m-%d")
        error_kind = type(exc).__name__
        sqlite_code = getattr(exc, "sqlite_errorcode", None)
        if type(sqlite_code) is int:
            error_kind += f" (sqlite_code={sqlite_code})"
        print(f"[collect] 读取 {db_type} {day} 失败: {error_kind}")
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
            platforms=platforms,
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
    stats["selected_dataflow"] = sum(
        1
        for x in selected
        if is_market_dataflow_title(x.get("title") or "", x.get("platforms"))
    )
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
