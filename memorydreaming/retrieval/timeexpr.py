"""中文时间表达解析：把「昨天」「上周三」「10月20日」变成明确的时间范围。

为什么这件事必须做对？
架构文档第 5 节讲得很清楚：
  - 48 小时影响的是「默认展开到什么细节」，不是「禁止检索旧事」；
  - 「第一次给你取名字时说的原话」即使发生在半年前，也要能精确回查。

所以这个模块只负责把时间说清楚，检索那边再决定怎么用：
  hard=True  是硬约束，明确日期就只在那个范围里找；
  hard=False 是模糊范围（「上次」），可以扩大范围再靠关键词定位。

第一版用规则解析，不接模型。规则的好处是完全可测、结果可解释；
以后接模型时，让它输出同样结构的 TimeHint 即可替换。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, tzinfo

from ..clock import day_bounds_utc, ensure_utc
from ..models import TimeHint

# 中文数字（只处理到十位，足够表达「最近三天」这类说法）
_CN_NUM = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 「最近三天」「最近 3 天」都能识别
_RECENT_RE = re.compile(r"(?:最近|近|过去|这)\s*([0-9]+|[一二两三四五六七八九十])\s*(天|日|周|个?月)")
# 「2026-10-20」「2026年10月20日」
_FULL_DATE_RE = re.compile(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?")
# 「10月20日」
_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _to_int(text: str) -> int | None:
    """把「3」或「三」转成整数。"""
    if text.isdigit():
        return int(text)
    return _CN_NUM.get(text)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _prev_month_start(day: date) -> date:
    first = _month_start(day)
    return _month_start(first - timedelta(days=1))


def _week_start(day: date) -> date:
    """本周一。Python 的 weekday()：周一=0。"""
    return day - timedelta(days=day.weekday())


def parse_time_hint(text: str, now: datetime, tz: tzinfo) -> TimeHint | None:
    """解析文本里的时间意图。找不到就返回 None。

    参数:
        text: 用户问题原文。
        now:  当前时刻（带时区）。
        tz:   用户所在时区——「昨天」是以用户的日历为准，不是以 UTC 为准。
    """
    now = ensure_utc(now)
    today = now.astimezone(tz).date()

    # --- 1) 明确日期优先，避免被「昨天」这类泛词盖住 ---
    match = _FULL_DATE_RE.search(text)
    if match:
        year, month, day = (int(group) for group in match.groups())
        try:
            target = date(year, month, day)
        except ValueError:
            return None
        start, end = day_bounds_utc(target, tz)
        return TimeHint(match.group(0), start, end, "day", hard=True)

    match = _MONTH_DAY_RE.search(text)
    if match:
        month, day = int(match.group(1)), int(match.group(2))
        try:
            target = date(today.year, month, day)
        except ValueError:
            return None
        start, end = day_bounds_utc(target, tz)
        return TimeHint(match.group(0), start, end, "day", hard=True)

    # --- 2) 相对日：今天 / 昨天 / 前天 / 明天 ---
    # 「刚才」单独处理：它指的是最近一小段时间，不是「今天一整天」。
    # 精度不同会直接影响回查范围，这里按 2 小时窗口处理。
    for word in ("刚才", "刚刚", "这会儿"):
        if word in text:
            return TimeHint(word, now - timedelta(hours=2), now, "exact", hard=True)

    offset_map = [
        (("今天", "今日", "今天早上", "今天下午", "今晚"), 0, "day"),
        (("昨天", "昨日", "昨晚"), -1, "day"),
        (("前天",), -2, "day"),
        (("明天", "明日"), 1, "day"),
    ]
    for words, offset, precision in offset_map:
        for word in words:
            if word in text:
                start, end = day_bounds_utc(today + timedelta(days=offset), tz)
                return TimeHint(word, start, end, precision, hard=True)

    # --- 3) 最近 N 天/周/月 ---
    match = _RECENT_RE.search(text)
    if match:
        amount = _to_int(match.group(1))
        unit = match.group(2)
        if amount:
            if unit in ("天", "日"):
                start = now - timedelta(days=amount)
            elif unit == "周":
                start = now - timedelta(weeks=amount)
            else:
                start = now - timedelta(days=30 * amount)
            return TimeHint(match.group(0), start, now, "day", hard=True)

    # --- 4) 周 / 月 / 年 ---
    if "上周" in text or "上星期" in text:
        start_day = _week_start(today) - timedelta(days=7)
        start, _ = day_bounds_utc(start_day, tz)
        end, _ = day_bounds_utc(start_day + timedelta(days=7), tz)
        return TimeHint("上周", start, end, "week", hard=True)

    if "这周" in text or "本周" in text or "这星期" in text:
        start_day = _week_start(today)
        start, _ = day_bounds_utc(start_day, tz)
        end, _ = day_bounds_utc(start_day + timedelta(days=7), tz)
        return TimeHint("本周", start, end, "week", hard=True)

    if "上个月" in text or "上月" in text:
        start_day = _prev_month_start(today)
        start, _ = day_bounds_utc(start_day, tz)
        end, _ = day_bounds_utc(_month_start(today), tz)
        return TimeHint("上个月", start, end, "month", hard=True)

    if "去年" in text:
        start_day = date(today.year - 1, 1, 1)
        start, _ = day_bounds_utc(start_day, tz)
        end, _ = day_bounds_utc(date(today.year, 1, 1), tz)
        return TimeHint("去年", start, end, "year", hard=True)

    if "今年" in text:
        start, _ = day_bounds_utc(date(today.year, 1, 1), tz)
        end, _ = day_bounds_utc(date(today.year + 1, 1, 1), tz)
        return TimeHint("今年", start, end, "year", hard=True)

    # --- 5) 模糊指代：没有具体范围，但说明「不是现在」 ---
    for word in ("上次", "上回", "那次", "那一次", "之前", "以前", "过去", "第一次", "最初"):
        if word in text:
            # start/end 留空 = 让检索走关键词/实体通道，而不是按时间扫表
            return TimeHint(word, None, None, "coarse", hard=False)

    return None
