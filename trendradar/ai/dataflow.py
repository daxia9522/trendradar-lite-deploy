# coding=utf-8
"""模板化数据流标题识别。

用途：把模板化的行情、经济数据、业绩播报与叙事型新闻分开。

这类标题的共同特征是「同一播报模板 + 只有主体和数字在变」，例如
「上证指数早盘收报3876.38点，跌0.14%」、
「德国8月IFO商业现况指数 88.5，预期 87，前值 86.5。」、
「中兴通讯：2026年上半年净利润27.53亿元，同比下降45.57%」。

它们在任何基于标题字符串的聚簇里都会失控：
- 去掉日期后模板骨架高度重合，数字差异在相似度中权重过低；
- 日更会占用事件配额，周报会被合并成 span7d 的假事件簇并顶到榜首；
- 已实测出「涨5%」与「跌2.3%」、「澳洲CPI」与「德国PPI」，
  以及「中兴通讯」「周大生」「海油发展」三家公司被判为同一事件。

## 强特征 / 弱特征分档

纯标题字符串无法区分「机器流水播报」与「编辑加工过的解读」。
在 24 天生产语料（31,974 条去重标题）上实测：同一个
「净利润同比下降N%」出现在快讯流里是模板播报，出现在精选榜里
是选题（如「腾讯Q2营收同比增长11%超预期，算力采购大幅推高资本
开支至527.8亿元 | 财报见闻」）。因此判定分两档：

- STRONG：模板骨架无歧义，任何来源都判定；
- WEAK：叙事标题也可能命中，仅在快讯/滚动类来源启用。

`source` 省略时按保守口径处理（只用强特征），避免误伤叙事新闻。
"""

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 快讯/滚动类来源：机器流水播报的产地。
# 同时匹配来源 id 与展示名，两个调用点传入的都是展示名
# （日更 selector 传 source_name，周报传 id_to_name 映射后的 platform_name）。
#
# 当前 14 个配置来源中只有「华尔街见闻 快讯 / wallstreetcn-quick」命中，
# 由 test_only_quick_feed_is_gated_in_current_config 锁住。
# 注意「即时」不列为特征：中新网即时（chinanews-scroll）虽名为即时，
# 但实测其弱特征命中仅 4/7186 条且均带编辑叙事，应留在叙事池。
_WIRE_SOURCE_RE = re.compile(
    r"quick|wire|flash|7\s*[x×]\s*24|快讯|滚动|直播|实时",
    re.IGNORECASE,
)

# 强特征：模板骨架无歧义，命中任意一条即认定为行情/数据播报。
_STRONG_PATTERNS = (
    # --- 经济数据发布：预期/前值/初值 组合 ---
    # 要求"预期"后紧跟数值，以排除"密切留意通胀预期""上调支出预期"等叙事表达。
    r"预期\s*-?[\d.]+\s*[%万亿]*\s*[户台辆吨桶点]?\s*[，,、]\s*(?:前值|初值)",
    # 前值/初值 + 数字 + 任意计量单位。旧版只认 % 结尾，
    # 漏掉「前值 9.8万人。」「前值 -26.44亿美元。」一类（实测漏判 87%）。
    # 单位不做枚举：实测外币与特殊计量单位（亿欧元/亿英镑/亿日元/亿纽元/
    # 亿立方英尺）无法穷举，改用通用非数字单位槽。
    r"(?:前值|初值)\s*-?[\d,.]+\s*[^\d，。,；;]{0,10}\s*[。，,；;]",
    r"预期\s*-?[\d.]+\s*万?[户台辆吨桶]",
    # --- 债券招标与发行 ---
    r"投标倍数",
    r"边际利率",
    r"边际倍数",
    r"倍数预期",
    # 「进出口行发行1年期债券，规模120亿元，发行利率1.4058%」
    r"发行\s*\d+\s*年期[^。]{0,24}发行利率",
    r"发行利率\s*-?[\d.]+\s*%",
    # --- 收益率与基点 ---
    r"个基点",
    r"SHIBOR",  # 旧版写作 \bSHIBOR\b，中文语境下词边界不成立，实测命中 0 次
    # --- 指数点位播报 ---
    r"(?:收报|开盘报|收盘报)\s*-?[\d,.]+\s*点",
    r"报\s*-?[\d,.]+\s*点",
    # 「台湾证交所加权股价指数上涨1%，至43,552.65点。」
    r"指数[^。]{0,24}(?:涨|跌|上涨|下跌)\s*-?[\d.]+\s*%?\s*[，,]?\s*至\s*-?[\d,.]+\s*点",
    r"(?:早盘|夜盘|盘初|收盘|开盘)\s*(?:收)?(?:涨|跌|报)",
    # 「截至早盘收盘，日经225指数跌1.7%，东证指数跌2.2%。」
    r"截至(?:早盘|午盘|夜盘|收盘|北京时间)[^。]{0,16}(?:收盘|指数|报)",
    r"指数转(?:涨|跌)",
    # --- 期货与合约 ---
    r"主力合约\s*(?:涨|跌|大涨|大跌|上涨|下跌)",
    r"指数期货\s*(?:盘初|夜盘|收)",
    r"期货收(?:涨|跌)\s*-?[\d.]+",
    # 「夜盘期货开盘，原油、燃油跌逾3%」「商品期货开盘，纯碱、乙二醇主力合约涨超3%」
    r"(?:夜盘|日盘|商品|国债|股指)?\s*期货(?:开盘|收盘)\s*[，,]",
    # --- 货币与资金市场 ---
    r"融资余额\s*(?:增加|减少)",
    r"中间价报\s*-?[\d,.]+",
    # 「沪深两市今日成交额合计25231.03亿元」「沪深京三市成交额超2万亿元」
    r"(?:沪深|两市|沪深京|三市)[^。]{0,12}成交额[^。]{0,12}"
    r"(?:合计|突破|超|达|缩量|放量|[\d.]+\s*(?:亿|万亿))",
    # --- 经济指标发布体：指数 + 空格数字 ---
    r"指数\s+-?[\d.]+\s*[，,。]",
    # --- 大盘并列播报 ---
    r"(?:沪指|深成指|创业板指)[^。]{0,40}(?:跌幅居前|涨幅居前|下跌个股|上涨个股)",
    # --- 现货报价体 ---
    r"报\s*-?[\d,.]+\s*(?:美元|元|元人民币)\s*/\s*(?:桶|吨|克|盎司)",
)

# 弱特征：含数字的叙事标题也可能命中，仅对快讯/滚动类来源启用。
# 已实测的误伤样本（均来自精选榜，不应被打成模板流）：
#   「SpaceX首份财报出炉：二季度营收同比增长92% 资本开支飙升至184亿美元」
#   「腾讯Q2营收同比增长11%超预期，算力采购大幅推高资本开支至527.8亿元 | 财报见闻」
#   「逼近历史警戒线！美债长端融资成本飙升，30年期得标利率创2001年最高」
#   「油价跌幅扩大至4%！贝森特：明日美伊或达成协议以开放霍尔木兹海峡」
#   「中国7月CPI同比涨幅收窄至0.5%，PPI同比上涨3.5%」
_WEAK_PATTERNS = (
    # 上市公司业绩播报。旧版要求「净利润」紧邻数字，
    # 漏掉「净利润为110.8亿元」「净利润约6700万元」（实测漏判 35 条）。
    r"(?:净利润|归母净利润|净亏损)\s*(?:为|约|达)?\s*(?:亏损)?\s*-?[\d,.]+\s*[亿万]元",
    r"(?:净利润|营业收入|营收)[^，。]{0,24}同比(?:增长|下降|增加|减少)\s*-?[\d.]+\s*%",
    r"同比(?:扭亏为盈|由盈转亏)",
    r"拟\s*10\s*派\s*-?[\d.]+\s*元",
    r"得标利率",
    r"(?:涨|跌)幅(?:扩大|收窄|一度达|达)\s*至?\s*-?[\d.]+\s*%",
    r"收益率(?:涨|跌|上行|下行)",
)

_STRONG_RE = re.compile("|".join(_STRONG_PATTERNS), re.IGNORECASE)
_WEAK_RE = re.compile("|".join(_WEAK_PATTERNS), re.IGNORECASE)
_DIGIT_RE = re.compile(r"\d")


def is_wire_source(source: Any) -> bool:
    """判断来源是否为快讯/滚动类机器流水。"""
    text = str(source or "").strip()
    return bool(text) and bool(_WIRE_SOURCE_RE.search(text))


def _iter_source_labels(source: Any) -> Iterable[str]:
    """来源可能是单个标识，也可能是周报里的平台集合。"""
    if source is None:
        return ()
    if isinstance(source, (str, bytes)):
        return (str(source),)
    if isinstance(source, dict):
        return [str(x) for x in source.keys()]
    if isinstance(source, Iterable):
        return [str(x) for x in source]
    return (str(source),)


def is_market_dataflow_title(title: Any, source: Any = None) -> bool:
    """判断标题是否为模板化行情/经济数据播报。

    Args:
        title: 标题文本。
        source: 来源 id、展示名，或周报侧的来源集合。省略时只用强特征。
    """
    text = str(title or "").strip()
    if not text or not _DIGIT_RE.search(text):
        return False
    if _STRONG_RE.search(text):
        return True
    if any(is_wire_source(label) for label in _iter_source_labels(source)):
        return bool(_WEAK_RE.search(text))
    return False


def split_market_dataflow(
    items: Iterable[Dict[str, Any]],
    title_key: str = "title",
    source_key: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按行情流特征拆分条目列表，返回 (叙事型, 行情流)。"""
    narrative: List[Dict[str, Any]] = []
    dataflow: List[Dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict):
            title = item.get(title_key)
            source = item.get(source_key) if source_key else None
        else:
            title, source = None, None
        (dataflow if is_market_dataflow_title(title, source) else narrative).append(item)
    return narrative, dataflow


__all__ = [
    "is_market_dataflow_title",
    "is_wire_source",
    "split_market_dataflow",
]
