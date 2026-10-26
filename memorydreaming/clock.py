"""时间工具：统一时间来源、时区换算、UTC 字符串读写。

为什么单独一个模块？
架构文档第 5 节要求「记录时间与事件时间分开」「统一用 UTC 存储、保留源时区」，
并且「用显式传入的时钟处理衰减与测试」。把时间来源集中在这里，好处是：

1. 业务代码不直接调用 datetime.now()，测试时换成 FixedClock 就能复现
   「48 小时之前 / 之后」的行为，不必等真实时间流逝。
2. 时区换算只有一处实现，不会出现有的地方存本地时间、有的地方存 UTC。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Protocol

# UTC 是唯一允许入库的时间基准
UTC = timezone.utc

# 兜底的时区偏移表。
# Windows 版 Python 默认不带 IANA 时区数据库（需要额外装 tzdata 包），
# 这时 ZoneInfo("Asia/Shanghai") 会抛异常。为了不让项目在 Windows 上直接跑不起来，
# 这里对常用时区退回固定偏移。注意：固定偏移不处理夏令时，
# 生产环境请 `pip install tzdata` 来获得完整时区规则。
_FALLBACK_OFFSETS: dict[str, int] = {
    "Asia/Shanghai": 8,
    "Asia/Hong_Kong": 8,
    "Asia/Taipei": 8,
    "Asia/Tokyo": 9,
    "Asia/Seoul": 9,
    "Asia/Singapore": 8,
    "UTC": 0,
    "Europe/London": 0,
    "Europe/Berlin": 1,
    "America/New_York": -5,
    "America/Los_Angeles": -8,
}


def load_timezone(name: str) -> tzinfo:
    """按 IANA 名称取时区对象，取不到就退回固定偏移或 UTC。

    参数:
        name: 例如 "Asia/Shanghai"、"UTC"。
    """
    try:
        from zoneinfo import ZoneInfo  # 放在函数内导入，方便这里写降级逻辑

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 缺 tzdata、名字写错都走同一条降级路径
        offset_hours = _FALLBACK_OFFSETS.get(name)
        if offset_hours is None:
            return UTC
        return timezone(timedelta(hours=offset_hours), name)


def utcnow() -> datetime:
    """当前时间（带 UTC 时区）。

    仅作为 dataclass 的默认值使用。需要可测试的地方请显式传入 Clock。
    """
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """把任意 datetime 归一成带时区的 UTC 时间。

    约定：不带时区的 naive datetime 一律当作 UTC 解读。
    这样做是为了避免「隐式使用运行机器的本地时区」这种难以排查的 bug；
    如果你手上是本地时间，请先自己 astimezone() 或用 attach_timezone()。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def attach_timezone(value: datetime, tz: tzinfo) -> datetime:
    """把 naive datetime 当作 `tz` 时区的本地时间来解读，并转成 UTC。

    典型用途：用户说「昨天下午三点」，这句话里的三点是本地时间。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


def to_utc_iso(value: datetime | None) -> str | None:
    """datetime -> 可入库的 ISO8601 字符串（UTC，微秒精度）。

    统一格式的好处：字符串的字典序等价于时间先后，
    所以 SQL 里可以直接用 `ORDER BY occurred_start` 或做范围比较。
    """
    if value is None:
        return None
    return ensure_utc(value).isoformat(timespec="microseconds")


def from_utc_iso(text: str | None) -> datetime | None:
    """ISO8601 字符串 -> 带 UTC 时区的 datetime。空值原样返回 None。"""
    if text is None or text == "":
        return None
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


def day_bounds_utc(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """某一天在指定时区里的起止时刻，转成 UTC 返回。

    返回左闭右开区间 [start, end)：
    「昨天」= [昨天 00:00, 今天 00:00)，用 < end 判断就不会漏掉 23:59。
    """
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def hours_ago(now: datetime, hours: float) -> datetime:
    """`hours` 小时之前的时刻，用于计算 48 小时窗口。"""
    return ensure_utc(now) - timedelta(hours=hours)


class Clock(Protocol):
    """时钟接口。凡是需要「现在几点」的地方都依赖它，而不是直接读系统时间。"""

    def now(self) -> datetime:
        """返回当前时间（必须带时区）。"""
        ...


class SystemClock:
    """真实时钟：生产环境默认使用。"""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """固定时钟：测试与示例专用，可以手动往前走。

    用法：
        clock = FixedClock(datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
        clock.advance(days=3)      # 模拟时间流逝三天
    """

    def __init__(self, moment: datetime) -> None:
        self._moment = ensure_utc(moment)

    def now(self) -> datetime:
        return self._moment

    def advance(self, **delta: float) -> datetime:
        """把时钟往前推。支持 days / hours / minutes / seconds 等关键字。"""
        self._moment = self._moment + timedelta(**delta)
        return self._moment

    def set(self, moment: datetime) -> datetime:
        """直接跳到某个时刻，便于测试具体的日期边界。"""
        self._moment = ensure_utc(moment)
        return self._moment
