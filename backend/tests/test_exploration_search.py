"""Search parsing/relevance regressions and an isolated search->read->evidence loop."""
import asyncio
import json

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
    _, life, repo, agent, run = setup(tmp_path, model=model)
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
