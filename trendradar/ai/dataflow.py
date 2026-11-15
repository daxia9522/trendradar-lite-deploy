# coding=utf-8
"""识别模板化行情、经济数据和业绩播报，供周报区别于叙事新闻。

强特征适用于所有来源；弱特征仅用于快讯/滚动来源，避免误伤编辑解读。
省略 source 时只用强特征。相同模板但主体、数字不同，不应合并成同一事件。
"""

import re
from typing import Any, Iterable

# 同时支持来源 ID 和展示名；“即时”不作为特征，以保留编辑叙事。
_WIRE_SOURCE_RE = re.compile(
    r"quick|wire|flash|7\s*[x×]\s*24|快讯|滚动|直播|实时",
    re.IGNORECASE,
)

# 强特征：模板骨架无歧义，命中任意一条即认定为行情/数据播报。
_STRONG_PATTERNS = (
    # --- 经济数据发布：预期/前值/初值 组合 ---
    # 要求"预期"后紧跟数值，以排除"密切留意通胀预期""上调支出预期"等叙事表达。
    r"预期\s*-?[\d.]+\s*[%万亿]*\s*[户台辆吨桶点]?\s*[，,、]\s*(?:前值|初值)",
    # 用通用单位槽覆盖人数、金额等非百分比前值。
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
    r"SHIBOR",  # 中文紧邻英文时不能依赖 \b 词边界。
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
_WEAK_PATTERNS = (
    # 净利润和数值之间允许“为、约、达”等连接词。
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


__all__ = [
    "is_market_dataflow_title",
    "is_wire_source",
]
