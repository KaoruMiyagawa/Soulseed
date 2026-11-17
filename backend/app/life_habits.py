"""Infer recurring local-time activities from saved completions."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from sqlite3 import Connection


@dataclass(frozen=True)
class LifeHabit:
    activity_type: str
    activity_name: str
    period: str
    status: str
    matching_days: int
    observed_days: int

    @property
    def choice_bonus(self) -> float:
        maximum = {"established": 0.24, "weakening": 0.06}.get(self.status, 0.0)
        return maximum * self.matching_days / self.observed_days


def local_period(hour: int) -> str:
    if 5 <= hour < 12:
        return "早上"
    if 12 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


OBSERVATION_DAYS = 8


def _established(matches: int, total: int) -> bool:
    return matches >= 5 and matches / total >= 0.625


def infer_habits(db: Connection, character_id: str, *, as_of: str, zone: tzinfo,
                 activity_names: dict[str, str]) -> tuple[LifeHabit, ...]:
    rows = db.execute(
        """SELECT event_type, occurred_at FROM life_events
           WHERE character_id = ? AND status = 'completed' AND activity_status = 'completed'
             AND occurred_at <= ?
           ORDER BY occurred_at DESC, id DESC LIMIT 1024""",
        (character_id, as_of),
    )
    period_days: dict[str, set[date]] = defaultdict(set)
    activity_days: dict[str, set[date]] = defaultdict(set)
    matching_days: dict[tuple[str, str], set[date]] = defaultdict(set)
    for row in rows:
        local = datetime.fromisoformat(row["occurred_at"]).astimezone(zone)
        day, period = local.date(), local_period(local.hour)
        period_days[period].add(day)
        kind = row["event_type"]
        if kind in activity_names:
            activity_days[kind].add(day)
            matching_days[kind, period].add(day)

    habits = []
    for (kind, period), matches in matching_days.items():
        # A different activity in this period or the same activity at a different
        # time is evidence of change. A day with no relevant records is unknown.
        days = sorted(period_days[period] | activity_days[kind], reverse=True)
        observations = [day in matches for day in days[:2 * OBSERVATION_DAYS]]
        recent = observations[:OBSERVATION_DAYS]
        hits = sum(recent)
        if _established(hits, len(recent)):
            status = "established"
        elif any(
            _established(sum(window), len(window))
            for offset in range(1, OBSERVATION_DAYS + 1)
            if (window := observations[offset:offset + OBSERVATION_DAYS])
        ):
            status = "weakening"
        elif hits >= 3 and hits / len(recent) >= 0.5:
            status = "forming"
        else:
            continue
        habits.append(LifeHabit(kind, activity_names[kind], period, status, hits, len(recent)))

    order = {"established": 0, "weakening": 1, "forming": 2}
    return tuple(sorted(habits, key=lambda habit: (
        order[habit.status], -habit.matching_days, habit.activity_type, habit.period,
    )))
