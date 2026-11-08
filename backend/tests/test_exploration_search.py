"""Search parsing/relevance regressions and an isolated search->read->evidence loop."""
import asyncio
import json
from pathlib import Path

import pytest

from app.exploration_agent import ExplorationAgent
from app.config import Settings
from app.life_share import LifeShareService
from app.web_research import (
    DuckDuckGoSearchProvider, FallbackSearchProvider, HttpResponse, ReadPage,
    SearchResult, WebResearchError, WikimediaSearchProvider,
    is_page_relevant_to_topic, is_search_result_relevant_to_topic,
)
from .test_exploration_agent import setup


WHALE_TOPIC = "鲸鱼为什么需要唱歌？"
WHALE_QUERY = "鲸歌的功能 座头鲸 求偶 群体交流 研究"
WHALE_EXCERPT = (
    "许多哺乳动物通过发出声音来交流。声音交流可以达成诸多目的，比如求偶、警戒、"
    "指明食物来源、社交等。雄性在求偶时发出的声音可屏退其他雄性、吸引雌性。座头鲸的鲸歌"
)


def test_real_saved_chinese_result_survives_query_reformulation_without_accepting_unrelated_pages():
    # The saved result says "座头鲸的鲸歌"; the model's query and result carry
    # matching whale/song anchors even though the original question uses 唱歌.
    assert is_search_result_relevant_to_topic(WHALE_TOPIC, WHALE_QUERY, "哺乳动物", WHALE_EXCERPT)
    assert not is_search_result_relevant_to_topic(
        WHALE_TOPIC, WHALE_QUERY, "动画角色列表", "科幻动画里的鲸鱼座喷水球角色与鲸歌研究无关。"
    )


def test_chinese_english_and_cross_language_queries_require_matching_result_signals():
    en_query = "whale song humpback courtship communication research"
    title, excerpt = "Whale vocalization", "Humpback whale songs are used in social communication and courtship."
    assert is_search_result_relevant_to_topic("座头鲸为什么唱歌？", en_query, title, excerpt)
    assert is_search_result_relevant_to_topic("whale songs and communication", en_query, title, excerpt)
    assert not is_search_result_relevant_to_topic("座头鲸为什么唱歌？", en_query, "Speech origins", "Human language evolved from vocal gestures.")
    assert is_page_relevant_to_topic("座头鲸为什么唱歌？", en_query, title, excerpt)
    assert not is_page_relevant_to_topic("座头鲸为什么唱歌？", en_query, title, "A page about unrelated human speech origins.")


@pytest.mark.parametrize("html,expected", [
    (b'<div class="no-results">No results found</div>', 0),
    (b'<div class="new-search-layout"><a href="https://example.org">result</a></div>', "format"),
])
def test_duckduckgo_empty_and_unknown_markup_are_distinguished(html, expected):
    class ControlledFetcher:
        async def fetch(self, url):
            return HttpResponse(url, 200, {"content-type": "text/html; charset=utf-8"}, html)
    provider = DuckDuckGoSearchProvider(ControlledFetcher())
    if expected == "format":
        with pytest.raises(WebResearchError) as error:
            asyncio.run(provider.search("whale song research"))
        assert error.value.code == "search_format_changed"
    else:
        assert asyncio.run(provider.search("whale song research")) == []


def test_fallback_distinguishes_real_zero_results_from_failed_provider_and_uses_english_index():
    class EmptyPrimary:
        async def search(self, _query): return []
    class EmptyFallback:
        async def search(self, _query): return []
    assert asyncio.run(FallbackSearchProvider(EmptyPrimary(), EmptyFallback()).search("no such topic")) == []

    class ChallengedPrimary:
        async def search(self, _query): raise WebResearchError("search_challenge", "challenge")
    with pytest.raises(WebResearchError) as error:
        asyncio.run(FallbackSearchProvider(ChallengedPrimary(), EmptyFallback()).search("whale song"))
    assert error.value.code == "search_unavailable"

    chosen = []
    class EnglishIndex:
        async def search(self, query):
            chosen.append(query)
            return [SearchResult("Whale vocalization", "https://en.wikipedia.org/wiki/Whale_vocalization",
                                 "Humpback whale songs support communication.", "wikimedia_en", True)]
    rows = asyncio.run(FallbackSearchProvider(
        ChallengedPrimary(), EmptyFallback(), EnglishIndex()
    ).search("whale song humpback communication"))
    assert chosen == ["whale song humpback communication"]
    assert rows[0].provider == "wikimedia_en" and rows[0].relevant is True


def test_cross_language_search_fallback_reads_sources_and_finishes_with_evidence(tmp_path):
    class Model:
        def __init__(self): self.calls = []
        async def reply(self, messages):
            self.calls.append(json.loads(messages[-1]["content"]))
            observations = self.calls[-1]["observations"]
            if not observations:
                return json.dumps({"action": "search_web", "query": WHALE_QUERY}, ensure_ascii=False)
            last = observations[-1]
            if last["tool"] == "search_web" and last["status"] == "failed":
                return json.dumps({"action": "search_web", "query": "whale song humpback communication research"})
            if last["tool"] == "read_webpage" and last["status"] == "ok":
                return json.dumps({"action": "finish", "summary": "座头鲸的歌声包含有组织的声音序列，并可用于交流。",
                                   "claims": [{"text": "座头鲸会发出有组织的歌声。", "kind": "sourced",
                                               "source_ids": [last["evidence_id"]]}], "unanswered": []}, ensure_ascii=False)
            return json.dumps({"action": "finish", "summary": "证据不足", "claims": [], "unanswered": ["需要相关来源"]}, ensure_ascii=False)

    class Challenged:
        async def search(self, _query): raise WebResearchError("search_challenge", "challenge")
    class ChineseIndex:
        async def search(self, _query): return []
    class EnglishIndex:
        async def search(self, query):
            assert query == "whale song humpback communication research"
            return [SearchResult("Whale vocalization", "https://en.wikipedia.org/wiki/Whale_vocalization",
                                 "Humpback whale songs form structured sequences used in communication.", "wikimedia_en", True)]
    class Reader:
        async def read(self, url):
            return ReadPage("Whale vocalization", url, "2030-01-01T00:00:00+00:00",
                            "Humpback whale songs form structured sequences. Researchers study whale song and social communication.")

    model = Model()
    _, life, repo, agent, run = setup(tmp_path, model=model, topic=WHALE_TOPIC)
    agent.search = FallbackSearchProvider(Challenged(), ChineseIndex(), EnglishIndex())
    agent.reader = Reader()
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "completed"
    assert result["model_call_count"] == 3
    assert [(item["tool_name"], item["outcome"]) for item in result["evidence"]] == [
        ("search_web", "failed"), ("search_web", "success"), ("read_webpage", "success")]
    assert result["evidence"][0]["error_code"] == "search_unavailable"
    assert result["evidence"][1]["provider"] == "wikimedia_en"
    assert result["evidence"][1]["relevance"] == "relevant"
    assert result["evidence"][2]["page_relevance"] == "relevant"
    assert result["claims"][0]["source_ids"] == [result["evidence"][2]["id"]]
    assert result["life_event_id"]


def test_search_failure_never_creates_a_discovery_or_share_intent(tmp_path):
    class Model:
        async def reply(self, messages):
            payload = json.loads(messages[-1]["content"])
            if not payload["observations"]:
                return json.dumps({"action": "search_web", "query": "whale song research"})
            return json.dumps({"action": "finish", "summary": "No source found.", "claims": [], "unanswered": ["No relevant source."]})
    class Unavailable:
        async def search(self, _query): raise WebResearchError("search_unavailable", "offline")
    model = Model()
    _, life, repo, agent, run = setup(tmp_path, model=model)
    agent.search = Unavailable()
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "failed" and result["life_event_id"] is None
    assert result["error_code"] == "insufficient_evidence"
    assert LifeShareService(life).list_intents() == []


def saved_search_trace():
    return json.loads((Path(__file__).parent / "fixtures" / "exploration_search_relevance_trace.json").read_text(encoding="utf-8"))


def test_saved_false_positive_and_single_character_bridges_are_rejected():
    trace = saved_search_trace()
    candidate = trace["false_positive"]
    assert not is_search_result_relevant_to_topic(
        trace["topic"], trace["actions"][1]["query"], candidate["title"], candidate["snippet"])
    assert not is_search_result_relevant_to_topic(
        "海獭如何使用工具", "海盗 游戏 道具", "海盗游戏道具", "海盗游戏中的道具与角色技能。")
    assert not is_search_result_relevant_to_topic(
        "鲸鱼为什么需要唱歌？", "鲸鱼 唱歌 原因", "音乐游戏", "游戏角色需要唱歌才能升级。")
    assert is_search_result_relevant_to_topic(
        "海豚为什么跃出海面", "海豚 跃水 群体交流", "海豚跃水",
        "海豚跃出海面可与群体交流有关，研究比较了海豚跃水行为。")


class StrategyModel:
    def __init__(self, *, retry_query="whale song humpback communication research", invalid_citation=False,
                 first_query=WHALE_QUERY):
        self.payloads = []
        self.retry_query = retry_query
        self.invalid_citation = invalid_citation
        self.first_query = first_query

    async def reply(self, messages):
        payload = json.loads(messages[-1]["content"])
        self.payloads.append(payload)
        if not payload["observations"]:
            return json.dumps({"action": "search_web", "query": self.first_query}, ensure_ascii=False)
        last = payload["observations"][-1]
        if last["tool"] == "read_webpage" and last["status"] == "ok":
            source_id = last["source_evidence_id"] if self.invalid_citation else last["evidence_id"]
            return json.dumps({"action": "finish", "summary": "已获得关于鲸类歌声的资料。",
                               "claims": [{"text": "座头鲸发出有组织的歌声。", "kind": "sourced",
                                           "source_ids": [source_id]}], "unanswered": []}, ensure_ascii=False)
        if len(payload["search_history"]) == 1 and "search_web" in payload["available_actions"]:
            return json.dumps({"action": "search_web", "query": self.retry_query}, ensure_ascii=False)
        return json.dumps({"action": "finish", "summary": "没有足够相关正文。", "claims": [],
                           "unanswered": ["需要相关正文资料"]}, ensure_ascii=False)


class StrategySearch:
    def __init__(self, first_results=None, failure=None, first_error=None):
        self.queries = []
        self.first_results = first_results or []
        self.failure = failure
        self.first_error = first_error

    async def search(self, query):
        self.queries.append(query)
        if self.failure:
            raise WebResearchError("search_unavailable", "controlled unavailable")
        if len(self.queries) == 1:
            if self.first_error:
                raise WebResearchError(self.first_error, "controlled first failure")
            return self.first_results
        return [SearchResult("Whale vocalization", "https://en.wikipedia.org/wiki/Whale_vocalization",
                             "Humpback whale songs are used in courtship and social communication.", "wikimedia_en", True)]


class StrategyReader:
    def __init__(self, *, reject_all=False):
        self.urls = []
        self.reject_all = reject_all

    async def read(self, url):
        self.urls.append(url)
        text = ("Human speech and language origins concern gestures and human communication."
                if self.reject_all or not url.startswith("https://en.wikipedia.org/") else
                "Humpback whale song has structured sequences used in social communication. Researchers record whale songs with hydrophones.")
        return ReadPage("Whale vocalization", url, "2030-01-01T00:00:00+00:00", text)


@pytest.mark.parametrize("first_error", [None, "search_unavailable"])
def test_chinese_empty_search_switches_to_english_reads_and_finishes_in_three_decisions(tmp_path, first_error):
    first_query = saved_search_trace()["actions"][0]["query"]
    model = StrategyModel(first_query=first_query)
    search, reader = StrategySearch(first_error=first_error), StrategyReader()
    _, _, _, agent, run = setup(tmp_path, model=model, search=search, reader=reader,
                                topic=WHALE_TOPIC, exploration_max_steps=3)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "completed"
    assert result["model_call_count"] == 3 and result["tool_call_count"] == 3
    assert search.queries == [first_query, model.retry_query]
    second = model.payloads[1]
    assert second["search_strategy"]["required_query_language"] == "en"
    assert second["progress"]["remaining_steps_after_this"] == 1
    assert "search_web" in second["available_actions"]
    body = next(item for item in result["evidence"] if item["tool_name"] == "read_webpage")
    assert body["page_relevance"] == "relevant"
    assert result["claims"][0]["source_ids"] == [body["id"]]
    assert reader.urls == ["https://en.wikipedia.org/wiki/Whale_vocalization"]


def test_saved_anime_candidate_is_never_automatically_read_and_english_retry_succeeds(tmp_path):
    row = saved_search_trace()["false_positive"]
    bad = SearchResult(row["title"], row["url"], row["snippet"], "wikimedia_zh", True)
    model, search, reader = StrategyModel(), StrategySearch([bad]), StrategyReader()
    _, _, _, agent, run = setup(tmp_path, model=model, search=search, reader=reader, topic=WHALE_TOPIC)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "completed"
    assert result["evidence"][0]["relevance"] == "irrelevant"
    assert row["url"] not in reader.urls
    assert result["model_call_count"] == 3 and result["tool_call_count"] == 3


@pytest.mark.parametrize("retry", ["为什么鲸鱼需要唱歌？", "whale song 鲸鱼", "research"])
def test_server_rejects_non_english_retry_before_another_tool_call(tmp_path, retry):
    model, search = StrategyModel(retry_query=retry), StrategySearch()
    _, life, _, agent, run = setup(tmp_path, model=model, search=search, topic=WHALE_TOPIC)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "search_query_language_required"
    assert result["model_call_count"] == 2 and result["tool_call_count"] == 1
    assert len(search.queries) == 1 and result["life_event_id"] is None
    assert LifeShareService(life).list_intents() == []


def test_unrelated_chinese_body_can_retry_english_within_four_tool_calls(tmp_path):
    candidate = SearchResult("鲸歌", "https://example.org/chinese", "鲸歌的功能与座头鲸求偶和群体交流有关。")
    model, search, reader = StrategyModel(), StrategySearch([candidate]), StrategyReader()
    _, _, _, agent, run = setup(tmp_path, model=model, search=search, reader=reader, topic=WHALE_TOPIC)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "completed"
    assert result["model_call_count"] == 3 and result["tool_call_count"] == 4
    bodies = [item for item in result["evidence"] if item["tool_name"] == "read_webpage"]
    assert [item["page_relevance"] for item in bodies] == ["irrelevant", "relevant"]
    assert result["claims"][0]["source_ids"] == [bodies[1]["id"]]


@pytest.mark.parametrize("failure", ["unrelated_body", "search_unavailable", "invalid_citation"])
def test_retry_failures_do_not_create_discovery_or_share(tmp_path, failure):
    model = StrategyModel(invalid_citation=failure == "invalid_citation")
    search = StrategySearch(failure=failure == "search_unavailable")
    reader = StrategyReader(reject_all=failure == "unrelated_body")
    _, life, _, agent, run = setup(tmp_path, model=model, search=search, reader=reader, topic=WHALE_TOPIC)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "failed" and result["life_event_id"] is None
    assert result["model_call_count"] <= 4 and result["tool_call_count"] <= 4
    assert LifeShareService(life).list_intents() == []


def test_retry_requires_both_search_and_read_tool_budget(tmp_path):
    model, search = StrategyModel(), StrategySearch()
    _, _, _, agent, run = setup(tmp_path, model=model, search=search, topic=WHALE_TOPIC,
                                exploration_max_tool_calls=2)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["error_code"] == "insufficient_evidence" and result["tool_call_count"] == 1
    assert "search_web" not in model.payloads[1]["available_actions"]


def test_automatic_read_prefers_subject_focused_title_over_broad_first_result(tmp_path):
    broad = SearchResult("哺乳动物", "https://example.org/broad", WHALE_EXCERPT)
    focused = SearchResult("鲸歌", "https://en.wikipedia.org/wiki/Whale_vocalization", "座头鲸的鲸歌可用于求偶及群体交流。")
    class ChineseReader(StrategyReader):
        async def read(self, url):
            self.urls.append(url)
            return ReadPage("鲸歌", url, "2030-01-01T00:00:00+00:00", WHALE_EXCERPT)
    model, reader = StrategyModel(), ChineseReader()
    search = StrategySearch([broad, focused])
    _, _, _, agent, run = setup(tmp_path, model=model, search=search, reader=reader, topic=WHALE_TOPIC,
                                exploration_max_steps=2)
    result = asyncio.run(agent.run(run["id"], "big_fish"))
    assert result["status"] == "completed" and len(search.queries) == 1
    assert result["model_call_count"] == 2 and result["tool_call_count"] == 2
    assert reader.urls == [focused.url]
