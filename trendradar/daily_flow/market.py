"""日报常规行情报价识别。"""

import re

_NUMBER = r"-?\d[\d,]*(?:\.\d+)?"
_INDEX = r"(?:上证指数|沪指|深成指|创业板指|科创50指数|恒生指数|恒生科技指数|日经225指数|日经指数|道指|纳指|标普500指数)"
_SESSION = r"(?:今日|早盘|午盘|尾盘|夜盘|盘初|开盘|收盘)?"
_CHANGE = rf"(?:上涨|下跌|涨|跌)\s*{_NUMBER}\s*[%％]"
_QUOTES = re.compile(
    rf"(?:{_INDEX}\s*{_SESSION}(?:收报|开盘报|收盘报|报)\s*{_NUMBER}\s*点(?:\s*[，,]\s*{_CHANGE})?"
    rf"|{_INDEX}\s*{_SESSION}(?:收涨|收跌|上涨|下跌|涨|跌)\s*{_NUMBER}\s*[%％]"
    rf"|(?:现货黄金|现货白银|国际金价|布伦特原油|WTI原油)\s*报\s*{_NUMBER}\s*(?:美元|元)\s*/\s*(?:盎司|桶|克))"
    r"\s*[。.]?",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)\s*[%％]")


def is_routine_market_quote(title: str) -> bool:
    """完整报价模板中，涨跌达到3%视为异常行情。"""
    if not _QUOTES.fullmatch(title.strip()):
        return False
    return all(abs(float(value)) < 3 for value in _PERCENT.findall(title))
