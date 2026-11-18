import { Component, StrictMode, useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import "./home.css";
import "./chat.css";
import "./interactions.css";
import { ChatAvatar } from "./ChatAvatar";
import { ChatTranscript } from "./ChatTranscript";
import { AsyncChat, ChatEmpty } from "./AsyncChat";
import { confirmDeliveryAndSync } from "./proactiveDelivery";
import { createApiRequest } from "./apiRequest";
import { desktopApi, desktopTransport } from "./desktopApi";
import { ModelSettings, modelPath, type ModelStatus } from "./ModelSettings";
import { quotaAllowsStart, quotaMessage, type ExplorationQuota } from "./explorationQuota";

type Conversation = { id: string; title: string };
type Session = { user_id: string; character_id: string };
type ProactiveMessage = { id: string; content: string; status: "ready" | "delivered" | "read" };
type DeliveryRetry = { message: ProactiveMessage; conversationId: string };
type ExplorationEvidence = { id: string; tool_name: string; outcome: string; source_url: string | null; title: string | null; retrieved_at: string | null; excerpt: string | null; page_relevance?: string | null };
type Exploration = { model_call_count?: number; model_diagnostics?: { reservations: number | null; http_attempts: number | null; http_attempts_complete: boolean; provider_receipt: string; provider_billing: string }; id: string; status: "planned" | "active" | "completed" | "interrupted" | "failed" | "cancelled"; phase?: string; trigger_mode?: string; topic_basis?: string | null; duration_ms?: number | null; timings?: Record<string, { count: number; total_ms: number; last_ms: number }>; requested_topic: string | null; selected_topic: string | null; topic_origin: string; summary: string | null; claims: { text: string; kind: string; source_ids: string[] }[]; unanswered: string[]; error_code: string | null; evidence: ExplorationEvidence[] };
type ExplorationAutonomy = { enabled: boolean; paused_until: string | null; cooldown_hours: number; last_started_at: string | null; last_checked_at?: string | null; last_skip_reason?: string | null; current: Exploration | null; latest: Exploration | null; quota: ExplorationQuota };
type LifeEvent = { id: string; content: string; event_type: string; status: string; activity_status?: string | null; effective_status?: string | null; feeling?: string | null; location?: string | null; occurred_at?: string | null; planned_start_at?: string | null; planned_end_at?: string | null };
type LifeSnapshot = { state: { mood: string | null; timezone_id: string; current_activity: LifeEvent | null }; events: LifeEvent[]; pending_shares: unknown[] };
type View = "world" | "chat" | "diary" | "plan" | "exploration";
const desktop = import.meta.env.MODE === "desktop";
const API = desktop ? desktopApi : import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001/api";

const request = createApiRequest(API, () => window.dispatchEvent(new Event("soulseed-session-expired")),
  undefined, desktop ? desktopTransport : undefined);

class PageErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <main className="pairing" role="alert">
      <p>页面暂时无法显示，请返回首页后重试。</p>
      <button className="button-primary" type="button" onClick={() => {
        history.replaceState(null, "", location.pathname + location.search);
        this.setState({ failed: false });
      }}>返回首页</button>
    </main>;
    return this.props.children;
  }
}

function explorationFailureMessage(code: string): string {
  const reasons: Record<string, string> = {
    invalid_model_action: "模型返回的探索指令格式不正确，本次已停止。",
    "invalid_model_action:invalid_json": "模型返回的探索指令无法解析，本次已停止。",
    "invalid_model_action:missing_field": "模型返回的探索指令缺少必要信息，本次已停止。",
    "invalid_model_action:invalid_arguments": "模型返回的探索参数不符合要求，本次已停止。",
    "invalid_model_action:duplicate_field": "模型返回的探索指令存在重复字段，本次已停止。",
    "invalid_model_action:truncated_output": "模型输出未完整生成，本次已停止。",
    "invalid_model_action:unexpected_tool_calls": "模型返回了不支持的工具调用格式，本次已停止。",
    "invalid_model_action:unexpected_finish_reason": "模型未正常完成探索决策，本次已停止。",
    disallowed_tool: "模型请求了不允许的操作，本次已停止。",
    action_not_available: "剩余步骤或工具预算不足以执行该动作，探索已停止。",
    model_unavailable: "模型服务暂时无法完成探索决策，请检查配置或额度。",
    exploration_daily_quota_exhausted: "今日探索请求额度已用完，请在重置后继续。",
    exploration_quota_unavailable: "探索额度记录暂时无法核验，已暂停探索。",
    model_provider_rate_limited: "模型服务商返回限流或供应商额度限制，请稍后再试。",
    model_network_error: "连接模型服务失败，请检查网络。",
    model_timeout: "等待模型服务超时；服务商是否收到或计费未知。",
    model_configuration_error: "探索模型配置不可用，请检查模型设置。",
    exploration_model_unavailable: "探索模型未启用或尚未配置，请检查模型设置。",
    autonomous_paused: "自主探索已暂停，没有继续调用模型。",
    repeated_exploration_topic: "已有相同或明显重复的探索问题。",
    no_new_research_finding: "引用的资料和结论已有记录，本次没有形成新发现。",
    service_restarted: "服务重启时任务尚未完成，已保留原有记录。",
    service_stopped: "服务停止时任务尚未完成，已保留原有记录。",
    search_unavailable: "搜索服务暂时不可用，没有取得资料；这次探索未完成。",
    search_format_changed: "搜索服务返回了无法识别的格式，没有把它当成搜索结果。",
    search_challenge: "搜索服务暂时要求验证，没有取得可用资料。",
    search_query_language_required: "中文检索未取得有效正文，模型未按要求切换英文补查，本次已停止。",
    dns_failed: "网页域名解析失败，没有读取该网页。",
    proxy_connection_failed: "无法连接本机网页代理，没有读取该网页。",
    proxy_connect_failed: "本机网页代理未能建立安全连接，没有读取该网页。",
    tls_handshake_failed: "网页的 TLS 证书或安全握手未通过，没有读取该网页。",
    connect_failed: "无法连接公开网页，没有读取该网页。",
    http_status: "网页服务器拒绝或未能提供内容，没有读取该网页。",
    response_too_large: "网页超过安全读取上限，没有作为资料使用。",
    read_timeout: "等待网页正文超时，没有读取该网页。",
    fetch_timeout: "网页读取超过时间上限，没有作为资料使用。",
    mediawiki_api_unavailable: "百科正文接口暂时不可用，没有使用该页面作为资料。",
    mediawiki_api_invalid: "百科正文接口返回内容无法核验，没有使用该页面作为资料。",
    unsupported_content_type: "网页内容格式不受支持，没有使用该页面作为资料。",
    insufficient_page_text: "网页没有提取到足够正文，没有作为资料使用。",
    model_provider_error: "模型服务商未能完成请求，请检查服务状态或接口兼容性。",
    insufficient_evidence: "资料与主题不够相关，或没有形成有来源支持的结论；本次探索未完成。",
    no_saved_interest: "目前没有可用于自主选题的已确认兴趣、已保存目标或待查问题；这次没有调用模型，也没有进行搜索。你可以输入主题手动开始。",
    step_limit_reached: "已达到本次探索的步骤上限。",
    time_budget_exceeded: "已达到本次探索的时间上限。",
    cancelled_by_user: "探索已取消。",
  };
  return reasons[code] ?? "本次探索未能完成。";
}

function dateInTimezone(value: string | Date, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: timezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date(value));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("year")}-${part("month")}-${part("day")}`;
}

function ProactivePreview({ message, hasConversation, onVisible }: {
  message: ProactiveMessage; hasConversation: boolean; onVisible: (id: string) => void;
}) {
  const card = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!card.current || !window.IntersectionObserver) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) onVisible(message.id);
    }, { threshold: 0.25 });
    observer.observe(card.current);
    return () => observer.disconnect();
  }, [message.id, onVisible]);
  return <article className="assistant proactive-preview" ref={card}>
    <label>大肥鱼 · 站内消息</label><p>{message.content}</p>
    {!hasConversation && <small>创建会话后可接收这条消息。</small>}
  </article>;
}

function App({ characterId, userId }: { characterId: string; userId: string }) {
  const CONTACT = `/characters/${encodeURIComponent(characterId)}/contact`;
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Conversation | null>(null);
  const [inbox, setInbox] = useState<ProactiveMessage[]>([]);
  const [visibleInbox, setVisibleInbox] = useState<Set<string>>(new Set());
  const [proactive, setProactive] = useState<Record<string, ProactiveMessage>>({});
  const [deliveryRetries, setDeliveryRetries] = useState<Record<string, DeliveryRetry>>({});
  const receiving = useRef<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [explorationTopic, setExplorationTopic] = useState("");
  const [exploration, setExploration] = useState<Exploration | null>(null);
  const [explorationBusy, setExplorationBusy] = useState(false);
  const [autonomy, setAutonomy] = useState<ExplorationAutonomy | null>(null);
  const [quota, setQuota] = useState<ExplorationQuota | null>(null);
  const [explorationLoadError, setExplorationLoadError] = useState("");
  const [view, setView] = useState<View>(() => location.hash === "#chat" ? "chat" : "world");
  const selectionKey = "soulseed-active-chat:" + JSON.stringify([API, userId, characterId]);
  const [life, setLife] = useState<LifeSnapshot | null>(null);
  const [lifeError, setLifeError] = useState("");
  const [model, setModel] = useState<ModelStatus | null>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [modelLoadError, setModelLoadError] = useState("");
  const [autonomousMasterPaused, setAutonomousMasterPaused] = useState<boolean | null>(null);
  const onVisible = useCallback((id: string) => setVisibleInbox((ids) => new Set(ids).add(id)), []);
  useEffect(() => {
    if (!desktop) return;
    const controller = new AbortController();
    request<ModelStatus>(modelPath, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setModel(result);
    }).catch((error: Error) => {
      if (!controller.signal.aborted) setModelLoadError(error.message);
    });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (view === "chat" && location.hash !== "#chat") history.replaceState(null, "", "#chat");
    if (view !== "chat" && location.hash === "#chat") history.replaceState(null, "", location.pathname + location.search);
  }, [view]);

  useEffect(() => {
    if (!active) return;
    try { sessionStorage.setItem(selectionKey, active.id); } catch { /* Optional view preference. */ }
  }, [selectionKey, active?.id]);
  const refreshQuota = useCallback(() => {
    if (desktop) {
      void request<{ autonomous_paused: boolean }>("/desktop/runtime")
        .then(state => setAutonomousMasterPaused(state.autonomous_paused))
        .catch(() => setAutonomousMasterPaused(null));
    }
    return request<ExplorationAutonomy>(`/characters/${encodeURIComponent(characterId)}/exploration-autonomy`)
      .then((state) => {
        setExplorationLoadError("");
        setQuota(state.quota); setAutonomy(state);
        setExploration(state.current ?? state.latest);
      }).catch((error: Error) => { setQuota(null); setAutonomy(null); setExplorationLoadError(error.message); });
  }, [characterId]);

  useEffect(() => {
    void refreshQuota();
    if (view !== "exploration") return;
    const timer = window.setInterval(() => void refreshQuota(), 15000);
    return () => window.clearInterval(timer);
  }, [refreshQuota, view, exploration?.status]);

  useEffect(() => {
    request<LifeSnapshot>(`/characters/${encodeURIComponent(characterId)}/life`)
      .then((snapshot) => { setLife(snapshot); setLifeError(""); })
      .catch((e: Error) => setLifeError(e.message));
  }, [characterId]);

  useEffect(() => {
    if (!exploration || !["planned", "active"].includes(exploration.status)) return;
    const timer = window.setInterval(() => {
      request<Exploration>(`/characters/${encodeURIComponent(characterId)}/explorations/${exploration.id}`)
        .then(setExploration).catch((e: Error) => setError(e.message));
    }, 1400);
    return () => window.clearInterval(timer);
  }, [characterId, exploration?.id, exploration?.status]);

  const submitExploration = async (retryRunId?: string) => {
    if (explorationBusy || !quotaAllowsStart(quota)) return;
    setExplorationBusy(true); setError("");
    try {
      const run = await request<Exploration>(`/characters/${encodeURIComponent(characterId)}/explorations`, {
        method: "POST",
        body: JSON.stringify({
          ...(retryRunId ? { retry_run_id: retryRunId } : { topic: explorationTopic.trim() || null }),
          idempotency_key: crypto.randomUUID(),
        }),
      });
      setExploration(run); setExplorationTopic("");
    } catch (e) { setError((e as Error).message); }
    finally { setExplorationBusy(false); void refreshQuota(); }
  };

  const startExploration = (event: React.FormEvent) => {
    event.preventDefault();
    void submitExploration();
  };

  const cancelExploration = async () => {
    if (!exploration) return;
    try {
      const run = await request<Exploration>(`/characters/${encodeURIComponent(characterId)}/explorations/${exploration.id}/cancel`, { method: "POST" });
      setExploration(run);
    } catch (e) { setError((e as Error).message); }
  };

  const updateAutonomy = async (update: { enabled?: boolean; paused_until?: string | null; cooldown_hours?: number }) => {
    try {
      const state = await request<ExplorationAutonomy>(`/characters/${encodeURIComponent(characterId)}/exploration-autonomy`, {
        method: "PUT", body: JSON.stringify(update),
      });
      setAutonomy(state);
      void refreshQuota();
    } catch (e) { setError((e as Error).message); }
  };

  const deliverAndSync = useCallback(async (message: ProactiveMessage, conversationId: string) => {
    if (receiving.current.has(message.id)) return;
    receiving.current.add(message.id);
    try {
      const { delivered, conversation } = await confirmDeliveryAndSync<ProactiveMessage, Conversation>(message, conversationId, CONTACT, request);
      setProactive((items) => ({ ...items, [message.id]: delivered }));
      // The server conversation is the source of truth. Refreshing it replaces
      // local state and makes retries safe after a lost acknowledgement.
      setActive((current) => current?.id === conversationId ? conversation : current);
      setInbox((items) => items.filter((item) => item.id !== message.id));
      setDeliveryRetries((items) => { const next = { ...items }; delete next[message.id]; return next; });
      setError("");
    } catch (e) {
      setDeliveryRetries((items) => ({ ...items, [message.id]: { message, conversationId } }));
      setError((e as Error).message);
    } finally {
      receiving.current.delete(message.id);
    }
  }, [CONTACT]);

  useEffect(() => {
    const controller = new AbortController();
    request<Conversation[]>("/conversations", { signal: controller.signal })
      .then(items => {
        if (controller.signal.aborted) return;
        let selected: string | null = null;
        try { selected = sessionStorage.getItem(selectionKey); } catch { /* Use the newest conversation. */ }
        setConversations(items);
        setActive(current => current ?? items.find(item => item.id === selected) ?? items[0] ?? null);
      }).catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    if (desktop) return () => controller.abort();
    const load = async () => {
      await request<{ ready_message_id: string | null }>(`${CONTACT}/check`, { method: "POST", signal: controller.signal });
      const [ready, history] = await Promise.all([
        request<ProactiveMessage[]>(`${CONTACT}/inbox`, { signal: controller.signal }),
        request<ProactiveMessage[]>(`${CONTACT}/messages`, { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      setInbox(ready);
      setProactive(Object.fromEntries(history.map((item) => [item.id, item])));
    };
    load().catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    const refresh = () => {
      if (document.visibilityState === "visible") {
        request<{ ready_message_id: string | null }>(`${CONTACT}/check`, { method: "POST", signal: controller.signal })
          .then(() => request<ProactiveMessage[]>(`${CONTACT}/inbox`, { signal: controller.signal }))
          .then(items => { if (!controller.signal.aborted) setInbox(items); })
          .catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
      }
    };
    document.addEventListener("visibilitychange", refresh);
    return () => {
      controller.abort();
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [CONTACT, selectionKey]);

  // React runs this after the ready card is visible. Server delivery is idempotent.
  useEffect(() => {
    if (!active) return;
    const item = inbox.find((entry) => visibleInbox.has(entry.id));
    if (!item) return;
    void deliverAndSync(item, active.id);
  }, [active?.id, inbox, visibleInbox, deliverAndSync]);

  const newConversation = async () => {
    const created = await request<Conversation>("/conversations", { method: "POST", body: JSON.stringify({}) });
    setConversations((items) => [created, ...items]);
    setActive(created);
  };

  const markRead = async (id: string) => {
    try {
      const updated = await request<ProactiveMessage>(`${CONTACT}/messages/${id}/read`, { method: "POST" });
      setProactive((items) => ({ ...items, [id]: updated }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const transitionLifeEvent = async (eventId: string, target: "active" | "completed" | "cancelled") => {
    try {
      await request<LifeEvent>(`/characters/${encodeURIComponent(characterId)}/life/events/${eventId}/transition`, {
        method: "POST", body: JSON.stringify({ target, reason: "manual" }),
      });
      setLife(await request<LifeSnapshot>(`/characters/${encodeURIComponent(characterId)}/life`));
      setLifeError("");
    } catch (e) { setLifeError((e as Error).message); }
  };

  const timezone = life?.state.timezone_id || "Asia/Shanghai";
  const today = dateInTimezone(new Date(), timezone);
  const plannedEvents = life?.events.filter((event) => event.effective_status === "planned" || event.activity_status === "planned") ?? [];
  const todayPlannedEvents = plannedEvents.filter((event) => event.planned_start_at && dateInTimezone(event.planned_start_at, timezone) === today);
  const recentEvents = life?.events.filter((event) => (event.effective_status === "completed" || event.status === "completed") && event.occurred_at && dateInTimezone(event.occurred_at, timezone) === today) ?? [];
  const currentActivity = life?.state.current_activity;
  const navItems: { id: View; label: string }[] = [
    { id: "world", label: "她的世界" }, { id: "chat", label: "对话" },
    { id: "diary", label: "手帐" }, { id: "plan", label: "计划" }, { id: "exploration", label: "探索" },
  ];

  const lifeStatus = <div className="life-status" aria-live="polite"><span className="status-dot" />{lifeError ? "生活状态暂不可用" : currentActivity?.content ?? (life ? "暂无进行中的活动" : "正在同步生活状态")}</div>;
  const header = <header className="site-header">
    <button className="wordmark" type="button" onClick={() => setView("world")} aria-label="返回她的世界">
      <span className="wordmark-name">soulseed <img className="brand-mark" src="/brand/Soulseed-Light.png" alt="" /></span>
      <span className="wordmark-caption">HER WORLD / ACTIVITY</span>
    </button>
    <nav className="primary-nav" aria-label="主导航">
      {navItems.map((item) => <button key={item.id} type="button" className={view === item.id ? "is-active" : ""} aria-current={view === item.id ? "page" : undefined} onClick={() => setView(item.id)}>{item.label}</button>)}
    </nav>
    {desktop ? <div className="desktop-settings-area">
      {lifeStatus}
      <button className="text-button" type="button" onClick={() => setModelOpen(true)}>
        {modelLoadError ? "模型设置 · 配置不可用" : model?.configured ? "模型设置" : "模型设置 · 未配置"}
      </button>
      {modelOpen && <ModelSettings status={model} request={request} characterId={characterId} onClose={() => setModelOpen(false)}
        onChange={status => { setModel(status); setModelLoadError(""); }} />}
    </div> : lifeStatus}
  </header>;

  if (view === "world") return <main className="soulseed-app">
    {header}
    <div className="world-page">
      <section className="world-grid">
        <div className="world-intro">
          <p className="eyebrow">ENTER HER WORLD</p>
          <h1>正在进入<br /><em>大肥鱼的</em><br />生活世界。</h1>
          <p className="world-description">这里是大肥鱼的日常、探索与成长之地。跟随她的脚步，一起生活、发现与体验她眼中的广阔世界吧。</p>
          <div className="intro-actions"><button className="button-primary" type="button" onClick={() => setView("plan")}>进入今日活动 <span aria-hidden="true">→</span></button><button className="button-secondary" type="button" onClick={() => setView("diary")}>世界导览</button></div>
        </div>

        <section className="room-stage" aria-label="大肥鱼的生活世界">
          <img className="room-scene" src="/assets/soulseed-hero.webp" alt="大肥鱼在鲸海小屋中抱着鲸鱼玩偶，蓝色长发与鱼尾映着海窗的阳光。" draggable={false} />
          <div className="scene-caption"><span>DA FEIYU’S LIVING WORLD</span><strong>大肥鱼的生活世界</strong><small>探索 · 发现 · 记录 · 一起成长</small></div>
          <button className="scene-action" type="button" onClick={() => setView("chat")} aria-label="与大肥鱼对话">↗</button>
        </section>

        <aside className="world-sidebar" aria-label="生活概览">
          <section className="overview-panel">
            <div className="panel-heading"><h2><span aria-hidden="true">▦</span> 今日活动 / TODAY</h2><span>{life ? new Date().toLocaleDateString("zh-CN", { timeZone: timezone, month: "long", day: "numeric", weekday: "short" }) : "同步中"}</span></div>
            {lifeError ? <p className="panel-empty">暂时无法读取她的活动。</p> : currentActivity ? <div className="activity-row is-current"><span className="activity-check">✓</span><span>{currentActivity.content}</span></div> : recentEvents.slice(0, 2).map((event) => <div className="activity-row" key={event.id}><span className="activity-check is-done">✓</span><span>{event.content}</span></div>)}
            {!lifeError && !currentActivity && recentEvents.length === 0 && <p className="panel-empty">目前还没有记录今天的活动。</p>}
            {todayPlannedEvents.slice(0, 1).map((event) => <div className="activity-row" key={event.id}><span className="activity-check is-open" /><span>{event.content}</span></div>)}
          </section>

          <section className="overview-panel mood-panel"><div className="panel-heading"><h2><span aria-hidden="true">♡</span> 当前心情</h2><button type="button" onClick={() => setView("diary")}>详情 ›</button></div>
            <div className="mood-content"><span className="mood-mark" aria-hidden="true">{life?.state.mood ? "✧" : "·"}</span><div><strong>{life?.state.mood || (life ? "尚未记录" : "同步中")}</strong><p>{currentActivity?.content || "查看她最近留下的生活记录"}</p></div></div>
          </section>

          <section className="overview-panel plan-panel"><div className="panel-heading"><h2><span aria-hidden="true">☷</span> 今日计划 <small>{todayPlannedEvents.length ? `${todayPlannedEvents.length} 项` : ""}</small></h2><button type="button" onClick={() => setView("plan")}>管理 ›</button></div>
            {todayPlannedEvents.slice(0, 3).map((event) => <div className="activity-row" key={event.id}><span className="activity-check is-open" /><span>{event.content}</span></div>)}
            {todayPlannedEvents.length === 0 && <p className="panel-empty">暂无今天已安排时间的计划。</p>}
          </section>
        </aside>
      </section>
      <nav className="world-links" aria-label="生活空间快捷入口">
        <button type="button" onClick={() => setView("chat")}><span className="quick-icon" aria-hidden="true">♡</span><span><strong>关系</strong><small>在对话中继续相处</small></span><span className="quick-arrow">›</span></button>
        <button type="button" onClick={() => setView("chat")}><span className="quick-icon" aria-hidden="true">▤</span><span><strong>回忆</strong><small>聊天时按需回忆已保存内容</small></span><span className="quick-arrow">›</span></button>
        <button type="button" onClick={() => setView("world")}><span className="quick-icon" aria-hidden="true">◎</span><span><strong>世界</strong><small>大肥鱼所生活的广阔世界</small></span><span className="quick-arrow">›</span></button>
        <button type="button" onClick={() => setView("diary")}><span className="quick-icon" aria-hidden="true">◉</span><span><strong>实时动态</strong><small>她此刻正在做什么？</small></span><span className="quick-arrow">›</span></button>
      </nav>
    </div>
  </main>;

  if (view === "diary") return <main className="soulseed-app">{header}<section className="content-page journal-page"><header className="view-heading"><div><p className="eyebrow">HER DAYS, AS THEY HAPPEN</p><h1>生活手帐</h1></div><span>来自已保存的生活事件</span></header>{lifeError && <p className="error" role="alert">{lifeError}</p>}{life?.events.length ? <div className="journal-list">{life.events.map((event) => <article className="journal-entry" key={event.id}><span className="entry-date">{event.occurred_at ? new Date(event.occurred_at).toLocaleDateString("zh-CN", { timeZone: timezone, month: "short", day: "numeric" }) : "计划中"}</span><div><h2>{event.content}</h2><p>{event.feeling || event.location || event.event_type}</p></div><span className="entry-status">{event.effective_status || event.status}</span></article>)}</div> : <p className="panel-empty">{life ? "这里还没有生活记录。" : "正在读取生活记录…"}</p>}</section></main>;

  if (view === "plan") return <main className="soulseed-app">{header}<section className="content-page journal-page"><header className="view-heading"><div><p className="eyebrow">A DAY AT HER OWN PACE</p><h1>今日计划</h1></div><span>已保存的活动安排</span></header>{lifeError && <p className="error" role="alert">{lifeError}</p>}{currentActivity && <article className="plan-current"><span className="eyebrow">正在进行</span><h2>{currentActivity.content}</h2><button className="button-secondary" type="button" onClick={() => void transitionLifeEvent(currentActivity.id, "completed")}>标记完成</button></article>}{plannedEvents.length ? <div className="journal-list">{plannedEvents.map((event) => <article className="journal-entry" key={event.id}><span className="entry-date">{event.planned_start_at ? new Date(event.planned_start_at).toLocaleTimeString("zh-CN", { timeZone: timezone, hour: "2-digit", minute: "2-digit" }) : "待安排"}</span><div><h2>{event.content}</h2><p>{event.location || "已保存的计划"}</p></div><span className="plan-actions"><button className="text-button" type="button" onClick={() => void transitionLifeEvent(event.id, "active")}>开始</button><button className="text-button" type="button" onClick={() => void transitionLifeEvent(event.id, "cancelled")}>取消</button></span></article>)}</div> : <p className="panel-empty">目前没有已保存的计划。</p>}</section></main>;

  if (view === "exploration") return <main className="soulseed-app">{header}<section className="content-page explore-page"><header className="view-heading"><div><p className="eyebrow">CURIOSITY, AT HER OWN PACE</p><h1>探索与发现</h1></div><span>研究任务与自主探索状态</span></header>
    {explorationLoadError && <p className="error" role="alert">探索状态读取失败：{explorationLoadError}</p>}
    <p role="status">{quotaMessage(quota)}</p><form className="explore-form" onSubmit={startExploration}><input aria-label="探索主题" value={explorationTopic} onChange={(e) => setExplorationTopic(e.target.value)} maxLength={240} placeholder="输入一个主题；留空时从已保存兴趣中选题" disabled={!quotaAllowsStart(quota) || explorationBusy || !!exploration && ["planned", "active"].includes(exploration.status)} /><button className="button-primary" disabled={!quotaAllowsStart(quota) || explorationBusy || !!exploration && ["planned", "active"].includes(exploration.status)}>{explorationBusy ? "安排中…" : "开始探索"}</button></form>
    <section className="autonomy-panel"><div><p className="eyebrow">BACKGROUND EXPLORATION</p><h2>后台自主探索：{autonomy ? autonomy.enabled ? "已启用" : "已关闭" : "状态未知"}{desktop && autonomousMasterPaused ? "（总暂停中）" : ""}</h2><p>{!autonomy ? "当前后端未返回设置状态。" : `后端运行时最多每 ${autonomy.cooldown_hours} 小时一次`}{autonomy?.paused_until && ` · 暂停至 ${new Date(autonomy.paused_until).toLocaleString()}`}</p>{desktop && autonomousMasterPaused && <small>模型设置中的自主探索总开关仍处于暂停状态。</small>}{autonomy?.last_skip_reason && <small>最近检查未启动：{({ no_eligible_topic: "没有符合条件的已保存兴趣、目标或待查问题", cooldown: "仍在冷却时间内", paused: "自主探索已暂停", active_task: "已有探索任务进行中", exploration_daily_quota_exhausted: "今日探索请求额度已用完，重置后再检查", exploration_quota_unavailable: "探索额度记录暂时无法核验" } as Record<string, string>)[autonomy.last_skip_reason] ?? "未满足启动条件"}</small>}</div><div className="autonomy-actions"><button className="button-secondary" type="button" disabled={!autonomy} onClick={() => updateAutonomy({ enabled: !autonomy?.enabled })}>{autonomy?.enabled ? "关闭自主探索" : "启用自主探索"}</button><label>探索间隔<select aria-label="自主探索间隔" disabled={!autonomy} value={autonomy?.cooldown_hours ?? 24} onChange={(event) => updateAutonomy({ cooldown_hours: Number(event.target.value) })}><option value={24}>每天最多一次</option><option value={48}>每两天一次</option><option value={168}>每周一次</option></select></label>{autonomy?.enabled && <button className="text-button" type="button" onClick={() => updateAutonomy({ paused_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() })}>暂停 24 小时</button>}{autonomy?.paused_until && <button className="text-button" type="button" onClick={() => updateAutonomy({ paused_until: null })}>恢复</button>}</div></section>
    {exploration && <article className="exploration-result" aria-live="polite"><strong>探索状态：{exploration.error_code === "insufficient_evidence" || exploration.status === "completed" && (!exploration.claims?.length || !exploration.evidence?.some((item) => item.tool_name === "read_webpage" && item.page_relevance === "relevant")) ? "资料不足，未形成可靠结论" : ({ planned: "计划中", active: "进行中", completed: "已完成", interrupted: "已中断", failed: "失败", cancelled: "已取消" } as const)[exploration.status]}</strong><span> · {exploration.selected_topic || exploration.requested_topic || "等待自主选题"} · {exploration.trigger_mode === "autonomous" ? "大肥鱼自主发起" : exploration.topic_origin === "user" ? "你指定的主题" : "根据已保存兴趣选题"}</span>{exploration.topic_basis && <span> · 依据：{({ stable_interest: "稳定兴趣", unresolved_question: "未解决问题", planned_goal: "已保存目标", derived_question: "探索衍生问题", pending_selection: "等待依据选择" } as Record<string, string>)[exploration.topic_basis] ?? exploration.topic_basis}</span>}{exploration.status === "active" && <p>当前阶段：{({ planning: "选择主题", searching: "搜索资料", reading: "安全读取网页", evaluating: "核对证据", planned: "等待开始" } as Record<string, string>)[exploration.phase ?? ""] ?? "执行中"}</p>}{<small>决策尝试 {exploration.model_call_count ?? 0} 次；请求预留 {exploration.model_diagnostics?.reservations ?? "未知"} 次；HTTP 尝试 {exploration.model_diagnostics?.http_attempts ?? "未知"}{exploration.model_diagnostics?.http_attempts_complete === false ? "（仅可确认的次数）" : ""} 次；服务商收到或计费情况未知。</small>}{exploration.timings && <small>模型 {exploration.timings.model?.count ?? 0} 次 / {Math.round((exploration.timings.model?.total_ms ?? 0) / 1000)} 秒；搜索 {Math.round((exploration.timings.search?.total_ms ?? 0) / 1000)} 秒；网页读取 {Math.round((exploration.timings.read?.total_ms ?? 0) / 1000)} 秒；总计 {Math.round((exploration.duration_ms ?? 0) / 1000)} 秒。</small>}{["planned", "active"].includes(exploration.status) && <button className="text-button" type="button" onClick={cancelExploration}>取消探索</button>}{exploration.summary && <p>{exploration.summary}</p>}{exploration.claims?.map((claim, index) => <p key={`${index}-${claim.text}`}><small>{claim.kind === "inference" ? "推断" : "来源支持"}</small> {claim.text}</p>)}{exploration.unanswered?.length > 0 && <p>还没查清：{exploration.unanswered.join("；")}</p>}{exploration.evidence?.filter((item) => item.tool_name === "read_webpage" && item.outcome === "success").map((item) => <p key={item.id}><a href={item.source_url ?? undefined} target="_blank" rel="noreferrer">{item.title || item.source_url}</a><small> · {item.retrieved_at} · {item.page_relevance === "relevant" ? "与主题相关" : item.page_relevance === "irrelevant" ? "正文无关，仅留作审计" : "旧记录未评估正文相关性"}</small></p>)}{exploration.error_code && <p className="error">{explorationFailureMessage(exploration.error_code)} 没有记作已完成探索。</p>}</article>}
    {exploration?.status === "interrupted" && <div>
      <p>重试会创建一次新的探索并占用当前额度。原任务的进度和调用记录会保留。</p>
      <button className="text-button" type="button" disabled={explorationBusy || !quotaAllowsStart(quota)}
        onClick={() => void submitExploration(exploration.id)}>重新尝试</button>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}</section></main>;

  return <main className="soulseed-app">{header}<div className="content-page chat-layout">
    <aside className="conversation-sidebar" aria-label="对话导航">
      <div className="conversation-heading"><h2>会话</h2><span>{conversations.length} 段对话</span></div>
      <button className="button-primary new-chat" onClick={() => newConversation().catch((e: Error) => setError(e.message))}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>新会话
      </button>
      <nav aria-label="会话列表">{conversations.map((item) => <button className={item.id === active?.id ? "selected" : ""} aria-current={item.id === active?.id ? "page" : undefined} title={item.title} key={item.id} onClick={() => setActive(item)}>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-10a9 9 0 0 1 18 0Z" /><path d="M7 10h8M7 14h5" /></svg><span>{item.title}</span>
      </button>)}</nav>
      {conversations.length === 0 && <p className="conversation-caption">点击上方按钮，创建一段新的对话。</p>}
    </aside>
    <section className="chat-panel" aria-label="与大肥鱼对话">
      <header className="view-heading chat-heading">
        <div className="chat-identity"><ChatAvatar /><div><h1>大肥鱼</h1>
          <p className="chat-status" data-active={!lifeError && !!currentActivity} aria-live="polite"><span className="chat-status-dot" aria-hidden="true" />{lifeError ? "生活状态暂不可用" : currentActivity?.content ?? (life ? "暂无进行中的活动" : "正在同步生活状态")}</p>
        </div></div>
        <span className="chat-header-note">对话中会按需检索相关记忆</span>
      </header>
      {active ? <AsyncChat key={active.id} api={API} user={userId} character={characterId}
        conversation={active.id} request={request} extraCount={inbox.length + Object.keys(deliveryRetries).length}
        desktopModelReady={desktop ? !!model?.configured : undefined}
        error={error} isDelivered={id => proactive[id]?.status === "delivered"} markRead={markRead}>
        {inbox.map(message => <ProactivePreview key={message.id} message={message} hasConversation onVisible={onVisible} />)}
        {Object.values(deliveryRetries).filter(retry => retry.conversationId === active.id).map(({ message, conversationId }) =>
          <div className="error chat-notice" key={message.id}>主动消息状态待同步：{message.content}
            <button type="button" onClick={() => void deliverAndSync(message, conversationId)}>重试投递与同步</button>
          </div>)}
      </AsyncChat> : <ChatTranscript messageCount={0}>
        {error && <div className="error chat-notice" role="alert">{error}</div>}
        <ChatEmpty hasConversation={false} />
      </ChatTranscript>}
    </section>
  </div></main>;

}

function LocalSession() {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [pairing, setPairing] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const expired = () => setSession(null);
    window.addEventListener("soulseed-session-expired", expired);
    request<Session>("/auth/session", { signal: controller.signal })
      .then(result => { if (!controller.signal.aborted) setSession(result); })
      .catch((error: Error) => {
        if (!controller.signal.aborted) {
          setSession(null);
          if (desktop) setError(error.message);
        }
      })
      .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => {
      controller.abort();
      window.removeEventListener("soulseed-session-expired", expired);
    };
  }, []);
  const pair = async (event: React.FormEvent) => {
    event.preventDefault(); setPairing(true); setError("");
    try {
      const session = await request<Session>("/auth/session", {
        method: "POST", body: JSON.stringify({ secret }),
      });
      setSession(session);
    } catch (e) { setError((e as Error).message); }
    finally { setSecret(""); setPairing(false); }
  };
  if (checking) return <main className="pairing">正在确认本地会话…</main>;
  if (session) return <App key={JSON.stringify([session.user_id, session.character_id])}
    userId={session.user_id} characterId={session.character_id} />;
  if (desktop) return <main className="pairing"><p role="alert">{error || "本地服务连接中断，请关闭后重新打开 Soulseed。"}</p></main>;
  if (development) return <main className="pairing"><p role="alert">暂时无法连接本地后端。请确认服务可用后刷新。</p></main>;
  return <main className="pairing"><h1>进入 Soulseed</h1>
    <p>输入本地配对口令，打开你的角色空间。</p>
    <form onSubmit={pair}><input aria-label="本地配对口令" type="password" autoComplete="current-password" value={secret} onChange={(e) => setSecret(e.target.value)} required />
      <button disabled={pairing}>{pairing ? "正在配对…" : "配对"}</button></form>
    {error && <p role="alert">{error}</p>}
  </main>;
}

const development = !desktop && import.meta.env.VITE_SOULSEED_DEVELOPMENT_MODE === "true";
createRoot(document.getElementById("root")!).render(
  <StrictMode><PageErrorBoundary>{development ? <App userId="local" characterId="big_fish" /> : <LocalSession />}</PageErrorBoundary></StrictMode>,
);
