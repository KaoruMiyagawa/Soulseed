"""G0 regression: ambiguous/negated reports must not grow preferences."""

from datetime import UTC, datetime

import pytest

from app.life_feelings import SHARE_POSITIVE_TERMS, classify_feeling
from app.db import Database
from app.life import LifeRepository
from app.life_engine import LifeEngine
from app.life_share import LifeShareService


@pytest.mark.parametrize("feeling, expected", [
    ("开心", "positive"), ("喜欢", "positive"), ("愉快", "positive"),
    ("满足", "positive"), ("我很喜欢", "positive"),
    ("不喜欢", "negative"), ("不满足", "negative"),
    ("不愉快", "negative"), ("不太喜欢", "negative"),
    ("不怎么开心", "negative"), ("没有感到开心", "negative"),
    ("难过", "negative"), ("疲惫", "negative"),
    ("开心但是疲惫", "mixed"), ("喜欢但有点累", "mixed"),
    ("不喜欢但很开心", "mixed"),
    ("好像很开心", "unknown"), ("如果开心就好了", "unknown"),
    ("我朋友说他很开心", "unknown"), ("她觉得很喜欢", "unknown"),
    ("喜欢吗？", "unknown"), ("“开心”", "unknown"),
    ("平静", "unknown"), ("不讨厌", "unknown"),
    ("不是不喜欢", "unknown"), ("", "unknown"),
])
def test_conservative_feeling_classification(feeling, expected):
    assert classify_feeling(feeling) == expected


@pytest.mark.parametrize("feeling, expected", [
    ("有趣", "positive"), ("惊喜", "positive"),
    ("不有趣", "negative"), ("不惊喜", "negative"),
    ("不喜欢", "negative"), ("不满足", "negative"),
    ("开心但疲惫", "mixed"), ("听说很开心", "unknown"),
])
def test_share_uses_same_negation_boundary(feeling, expected):
    assert classify_feeling(feeling, positive_terms=SHARE_POSITIVE_TERMS) == expected


@pytest.mark.parametrize("feeling, expected_delta", [
    ("开心", 0.03), ("不喜欢", -0.03), ("不满足", -0.03),
    ("不愉快", -0.03), ("开心但疲惫", 0),
    ("我朋友说他很开心", 0), ("如果开心就好了", 0),
])
def test_completed_event_updates_weight_only_for_unambiguous_feeling(tmp_path, feeling, expected_delta):
    db = Database(str(tmp_path / "feedback.db"))
    db.initialize()
    life = LifeRepository(db)
    engine = LifeEngine(life)
    baseline = engine.preferences("big_fish")["music"]
    at = datetime(2026, 11, 18, 9, tzinfo=UTC)
    event = life.record("听音乐", event_type="music", feeling=feeling, now=at)
    engine.learn_from_completion(event, at)
    engine.learn_from_completion(event, at)  # Repeated delivery must be idempotent.
    assert round(engine.preferences("big_fish")["music"] - baseline, 2) == expected_delta
    with db.connection() as sql:
        count = sql.execute(
            "SELECT COUNT(*) FROM life_preference_changes WHERE event_id=? AND reason='completed_feeling'",
            (event["id"],),
        ).fetchone()[0]
    assert count == (1 if expected_delta else 0)


@pytest.mark.parametrize("feeling, expected_share", [
    ("喜欢", True), ("有趣", True), ("不喜欢", False),
    ("不满足", False), ("不有趣", False),
    ("开心但疲惫", False), ("如果开心就好了", False),
])
def test_life_share_never_promotes_negated_or_ambiguous_feelings(tmp_path, feeling, expected_share):
    db = Database(str(tmp_path / "share.db"))
    db.initialize()
    life = LifeRepository(db)
    LifeEngine(life).preferences("big_fish")
    event = life.record("听音乐", event_type="music", feeling=feeling)
    result = LifeShareService(life).consider(event["id"])
    assert (result is not None) == expected_share
