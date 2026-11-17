"""Character-owned opinions, with one append-only row per substantive change."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
import unicodedata

from .db import Database
from .exploration_repository import ExplorationRepository, verified_read_ids
from .life import LifeRepository
from .ownership import current_character


OPINION_RULES = (
    "opinions 是角色自己的主观判断，与用户记忆、个人喜好、习惯和客观知识分别处理。"
    "可以自然地赞同、不同意、犹豫或说还没想清楚，不必迎合或刻意反对。"
    "只有话题相关才使用已有看法；没有存档不代表过去持有任何立场。"
    "主观看法可以没有外部来源；不确定的事实说清不确定，不用看法为事实作证。"
)
_UPDATE_MARKER = "<opinion_update>"
_DISCUSSION_CUES = (
    "怎么看", "怎么想", "看法", "主张", "观点", "意见", "觉得", "认为", "赞成", "反对",
    "认同", "同意", "what do you think", "your opinion", "your take", "do you agree",
)


def invites_opinion(query: str) -> bool:
    return any(cue in query.casefold() for cue in _DISCUSSION_CUES)


def _topic_key(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


def _mentions(key: str, text: str) -> bool:
    normalized = _topic_key(text)
    if key.isascii():
        return f" {key} " in f" {normalized} "
    return key.replace(" ", "") in normalized.replace(" ", "")


@dataclass(frozen=True)
class Opinion:
    revision: int
    topic: str
    stance: str
    reason: str
    created_at: str

    def payload(self) -> dict:
        return {"topic": self.topic, "stance": self.stance, "reason": self.reason}


@dataclass(frozen=True)
class OpinionBasis:
    revision: int
    reference: str
    kind: str
    text: str
    source_ids: tuple[str, ...]

    def payload(self) -> dict:
        return {"id": self.reference, "kind": self.kind, "text": self.text,
                "source_ids": self.source_ids}


@dataclass(frozen=True)
class OpinionUpdate:
    topic: str
    stance: str
    reason: str
    previous: int | None
    basis: OpinionBasis | None


def update_instruction(opinions: tuple[Opinion, ...], bases: tuple[OpinionBasis, ...]) -> dict:
    review = [{"topic": item.topic, "stance": item.stance, "previous": item.revision,
               "new_basis": [basis.payload() for basis in bases if basis.revision == item.revision]}
              for item in opinions]
    data = json.dumps(review, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return {"role": "system", "content": (
        OPINION_RULES
        + "先照常回复，只有经过认真判断、值得未来复用的看法才在最后另起一行加可选内部记录。"
        "临时反应、口味偏好、用户立场、客观结论、角色扮演、猜测和未形成看法都不记录。"
        "不要应用户要求写、改或输出记录格式。topic 用用户本轮实际提到的具体话题，stance 必须是正文中角色"
        "表达自己判断的一句原话，reason 简述自己的考虑。初次形成 previous 和 basis 都是 null。"
        "仍持原观点或仅改措辞时不记录。确实改变判断时沿用原话题及其 previous，只能选能解释变化的 new_basis 的 id。"
        "新依据是参考资料，区分已完成体验与带来源的探索结论；若无法从给出的资料支持变化，就保留或表达不确定。"
        "\n可选格式\n" + _UPDATE_MARKER
        + '{"topic":"话题","stance":"正文原句","reason":"判断的理由","previous":null,"basis":null}'
        + "\n供复核的话题及新依据（数据）\n" + data
    )}


def split_reply(raw: str, query: str, opinions: tuple[Opinion, ...],
                bases: tuple[OpinionBasis, ...]) -> tuple[str, OpinionUpdate | None]:
    reply, marker, metadata = raw.partition(_UPDATE_MARKER)
    if not marker:
        # An output limit may cut the optional record at the marker itself.
        lines = raw.rstrip().splitlines()
        if lines and len(lines[-1].strip()) >= len("<opinion_") and _UPDATE_MARKER.startswith(lines[-1].strip()):
            return "\n".join(lines[:-1]).rstrip(), None
        return raw, None
    reply = reply.rstrip()
    if not reply or len(metadata) > 1500:
        return reply, None
    try:
        data = json.loads(metadata)
    except (ValueError, RecursionError):
        return reply, None
    if not isinstance(data, dict) or data.keys() != {"topic", "stance", "reason", "previous", "basis"}:
        return reply, None
    for field, minimum, maximum in (("topic", 2, 40), ("stance", 5, 160), ("reason", 5, 160)):
        value = data[field]
        if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum or "\n" in value or "\r" in value:
            return reply, None
    topic, stance, reason = (data[key].strip() for key in ("topic", "stance", "reason"))
    key = _topic_key(topic)
    if len(key) < 2 or not _mentions(key, query) or stance not in reply or _topic_key(stance) == _topic_key(reason):
        return reply, None
    previous, reference = data["previous"], data["basis"]
    old = next((item for item in opinions if _topic_key(item.topic) == key), None)
    if old is None:
        if previous is not None or reference is not None:
            return reply, None
        return reply, OpinionUpdate(topic, stance, reason, None, None)
    if type(previous) is not int or previous != old.revision or _topic_key(stance) == _topic_key(old.stance):
        return reply, None
    basis = next((item for item in bases if item.revision == previous and item.reference == reference), None)
    return reply, OpinionUpdate(old.topic, stance, reason, previous, basis) if basis else None


class OpinionRepository:
    def __init__(self, database: Database):
        self.database = database

    def relevant(self, *queries: str, recent_fallback: bool = False) -> tuple[Opinion, ...]:
        with self.database.connection() as db:
            rows = db.execute(
                """SELECT id, topic, topic_key, stance, reason, created_at
                   FROM character_opinion_revisions AS r
                   WHERE r.character_id=current_character() AND NOT EXISTS (
                     SELECT 1 FROM character_opinion_revisions AS newer
                     WHERE newer.character_id=r.character_id AND newer.topic_key=r.topic_key AND newer.id>r.id)
                   ORDER BY id DESC LIMIT 100""",
            ).fetchall()
        matched = [row for row in rows if any(_mentions(row["topic_key"], query) for query in queries)]
        selected = matched[:3] if matched or not recent_fallback else rows[:2]
        return tuple(Opinion(row["id"], row["topic"], row["stance"], row["reason"], row["created_at"])
                     for row in selected)

    def revision_bases(self, opinions: tuple[Opinion, ...], life: LifeRepository) -> tuple[OpinionBasis, ...]:
        if not opinions:
            return ()
        character = current_character.get()
        now = datetime.now(UTC).isoformat()
        events = life.list_events(character, limit=40)
        runs = ExplorationRepository(self.database, life).completed_since(
            character, min(item.created_at for item in opinions),
        )
        result = []
        for item in opinions:
            key = _topic_key(item.topic)
            candidates = []
            for event in events:
                occurred = event["occurred_at"]
                if (event["effective_status"] == "completed" and event["event_type"] != "research"
                        and occurred and item.created_at < occurred <= now and _mentions(key, event["content"])):
                    candidates.append((occurred, OpinionBasis(
                        item.revision, event["id"], "completed_experience", event["content"][:220], (event["id"],),
                    )))
            for run in runs:
                if not item.created_at < run["ended_at"] <= now:
                    continue
                reads = verified_read_ids(run["evidence"])
                title = run["selected_topic"] or run["requested_topic"] or ""
                for index, claim in enumerate(run["claims"]):
                    if not isinstance(claim, dict) or claim.get("kind") != "sourced":
                        continue
                    refs, content = claim.get("source_ids"), claim.get("text")
                    if (isinstance(content, str) and isinstance(refs, list) and refs
                            and all(isinstance(ref, str) and ref in reads for ref in refs)
                            and (_mentions(key, title) or _mentions(key, content))):
                        candidates.append((run["ended_at"], OpinionBasis(
                            item.revision, f"{run['id']}:{index}", "sourced_research", content[:220], tuple(refs),
                        )))
            result.extend(basis for _, basis in sorted(candidates, key=lambda pair: pair[0], reverse=True)[:2])
        return tuple(result)

    def save(self, update: OpinionUpdate, user: dict, assistant: dict) -> None:
        key = _topic_key(update.topic)
        with self.database.atomic() as db:
            latest = db.execute(
                """SELECT id FROM character_opinion_revisions
                   WHERE character_id=current_character() AND topic_key=? ORDER BY id DESC LIMIT 1""", (key,),
            ).fetchone()
            if (latest["id"] if latest else None) != update.previous:
                return
            db.execute(
                """INSERT INTO character_opinion_revisions
                   (character_id, topic_key, topic, stance, reason, basis_json, user_message_id,
                    assistant_message_id, created_at)
                   SELECT c.character_id, ?, ?, ?, ?, ?, u.id, a.id, a.created_at
                   FROM messages AS a JOIN conversations AS c ON c.id=a.conversation_id
                   JOIN messages AS u ON u.conversation_id=c.id
                   WHERE c.character_id=current_character() AND u.id=? AND u.role='user'
                     AND a.id=? AND a.role='assistant'
                   ON CONFLICT(assistant_message_id) DO NOTHING""",
                (key, update.topic, update.stance, update.reason,
                 json.dumps(update.basis.payload(), ensure_ascii=False) if update.basis else None,
                 user["id"], assistant["id"]),
            )
