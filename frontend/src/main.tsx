import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import "./home.css";
import { confirmDeliveryAndSync } from "./proactiveDelivery";
import { ApiError, createApiRequest } from "./apiRequest";

type Message = { id: string; role: "user" | "assistant" | "system"; content: string };
type Conversation = { id: string; title: string; messages?: Message[] };
type ProactiveMessage = { id: string; content: string; status: "ready" | "delivered" | "read" };
type DeliveryRetry = { message: ProactiveMessage; conversationId: string };
type ExplorationEvidence = { id: string; tool_name: string; outcome: string; source_url: string | null; title: string | null; retrieved_at: string | null; excerpt: string | null; page_relevance?: string | null };
type Exploration = { id: string; status: "planned" | "active" | "completed" | "interrupted" | "failed" | "cancelled"; phase?: string; trigger_mode?: string; topic_basis?: string | null; duration_ms?: number | null; timings?: Record<string, { count: number; total_ms: number; last_ms: number }>; requested_topic: string | null; selected_topic: string | null; topic_origin: string; summary: string | null; claims: { text: string; kind: string; source_ids: string[] }[]; unanswered: string[]; error_code: string | null; evidence: ExplorationEvidence[] };
type ExplorationAutonomy = { enabled: boolean; paused_until: string | null; cooldown_hours: number; last_started_at: string | null; last_checked_at?: string | null; last_skip_reason?: string | null; current: Exploration | null; latest: Exploration | null };
type LifeEvent = { id: string; content: string; event_type: string; status: string; activity_status?: string | null; effective_status?: string | null; feeling?: string | null; location?: string | null; occurred_at?: string | null; planned_start_at?: string | null; planned_end_at?: string | null };
type LifeSnapshot = { state: { mood: string | null; timezone_id: string; current_activity: LifeEvent | null }; events: LifeEvent[]; pending_shares: unknown[] };
type View = "world" | "chat" | "diary" | "plan" | "exploration";
const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001/api";

const request = createApiRequest(API, () => window.dispatchEvent(new Event("soulseed-session-expired")));

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
    insufficient_evidence: "资料与主题不够相关，或没有形成有来源支持的结论；本次探索未完成。",
    no_saved_interest: "目前没有可用于自主选题的已确认兴趣、已保存目标或待查问题；这次没有调用模型，也没有进行搜索。你可以输入主题手动开始。",
    step_limit_reached: "已达到本次探索的步骤上限。",
    time_budget_exceeded: "已达到本次探索的时间上限。",
    cancelled_by_user: "探索已取消。",
    service_stopped: "服务停止，探索已中断。",
    service_restarted: "服务重启，探索已中断。",
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

function App({ characterId }: { characterId: string }) {
  const CONTACT = `/characters/${encodeURIComponent(characterId)}/contact`;
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Conversation | null>(null);
  const [inbox, setInbox] = useState<ProactiveMessage[]>([]);
  const [visibleInbox, setVisibleInbox] = useState<Set<string>>(new Set());
  const [proactive, setProactive] = useState<Record<string, ProactiveMessage>>({});
  const [deliveryRetries, setDeliveryRetries] = useState<Record<string, DeliveryRetry>>({});
  const receiving = useRef<Set<string>>(new Set());
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [syncAfterFailure, setSyncAfterFailure] = useState<string | null>(null);
  const [explorationTopic, setExplorationTopic] = useState("");
  const [exploration, setExploration] = useState<Exploration | null>(null);
  const [explorationBusy, setExplorationBusy] = useState(false);
  const [autonomy, setAutonomy] = useState<ExplorationAutonomy | null>(null);
  const [view, setView] = useState<View>("world");
  const [life, setLife] = useState<LifeSnapshot | null>(null);
  const [lifeError, setLifeError] = useState("");
  const onVisible = useCallback((id: string) => setVisibleInbox((ids) => new Set(ids).add(id)), []);

  useEffect(() => {
    request<ExplorationAutonomy>(`/characters/${encodeURIComponent(characterId)}/exploration-autonomy`)
      .then((state) => { setAutonomy(state); setExploration(state.current ?? state.latest); })
      .catch((e: Error) => setError(e.message));
  }, [characterId]);

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

  const startExploration = async (event: React.FormEvent) => {
    event.preventDefault();
    if (explorationBusy) return;
    setExplorationBusy(true); setError("");
    try {
      const run = await request<Exploration>(`/characters/${encodeURIComponent(characterId)}/explorations`, {
        method: "POST",
        body: JSON.stringify({ topic: explorationTopic.trim() || null, idempotency_key: crypto.randomUUID() }),
      });
      setExploration(run); setExplorationTopic("");
    } catch (e) { setError((e as Error).message); }
    finally { setExplorationBusy(false); }
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
      setExploration(state.current ?? state.latest);
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
    const load = async () => {
      await request<{ ready_message_id: string | null }>(`${CONTACT}/check`, { method: "POST" });
      const [items, ready, history] = await Promise.all([
        request<Conversation[]>("/conversations"),
        request<ProactiveMessage[]>(`${CONTACT}/inbox`),
        request<ProactiveMessage[]>(`${CONTACT}/messages`),
      ]);
      setConversations(items);
      setInbox(ready);
      setProactive(Object.fromEntries(history.map((item) => [item.id, item])));
      if (items[0]) setActive(await request<Conversation>(`/conversations/${items[0].id}`));
    };
    load().catch((e: Error) => setError(e.message));
    const refresh = () => {
      if (document.visibilityState === "visible") {
        request<{ ready_message_id: string | null }>(`${CONTACT}/check`, { method: "POST" })
          .then(() => request<ProactiveMessage[]>(`${CONTACT}/inbox`))
          .then(setInbox).catch((e: Error) => setError(e.message));
      }
    };
    document.addEventListener("visibilitychange", refresh);
    return () => document.removeEventListener("visibilitychange", refresh);
  }, []);

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
    setActive({ ...created, messages: [] });
  };

  const send = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!input.trim() || !active || busy || syncAfterFailure) return;
    setBusy(true);
    setError("");
    const content = input;
    const conversationId = active.id;
    const started = performance.now();
    try {
      const message = await request<Message>(`/conversations/${conversationId}/messages`, {
        method: "POST", body: JSON.stringify({ content }),
      });
      setActive((conversation) => conversation?.id === conversationId
        ? { ...conversation, messages: [...(conversation.messages ?? []), { id: crypto.randomUUID(), role: "user", content }, message] }
        : conversation);
      setInput("");
      requestAnimationFrame(() => console.info("soulseed_chat_display", { nextFrameMs: performance.now() - started }));
    } catch (e) {
      const failure = e as Error;
      if (failure instanceof ApiError && failure.messageSaved) {
        setInput("");
        try {
          const refreshed = await request<Conversation>(`/conversations/${conversationId}`);
          setActive((current) => current?.id === conversationId ? refreshed : current);
          setInput("");
          setError(`模型未完成本次回复：${failure.message} 已同步会话记录；请直接继续发送，避免重复提交。`);
        } catch {
          setSyncAfterFailure(conversationId);
          setError(`${failure.message} 请先刷新当前会话确认记录，再继续发送。`);
        }
      } else if (!(failure instanceof ApiError)) {
        // A lost response is not evidence that the POST was not committed.
        // Clear the submitted draft and only offer a read-only resync, never a resend.
        setInput("");
        setSyncAfterFailure(conversationId);
        setError("连接或响应异常，本次发送状态未知，模型可能仍在处理。请同步会话，不要原样重发。");
      } else {
        setError(failure.message);
      }
    } finally {
      setBusy(false);
    }
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

  const header = <header className="site-header">
    <button className="wordmark" type="button" onClick={() => setView("world")} aria-label="返回她的世界">
      <span className="wordmark-name">soulseed <span aria-hidden="true">✧</span></span>
      <span className="wordmark-caption">HER WORLD / ACTIVITY</span>
    </button>
    <nav className="primary-nav" aria-label="主导航">
      {navItems.map((item) => <button key={item.id} type="button" className={view === item.id ? "is-active" : ""} aria-current={view === item.id ? "page" : undefined} onClick={() => setView(item.id)}>{item.label}</button>)}
    </nav>
    <div className="life-status" aria-live="polite"><span className="status-dot" />{lifeError ? "生活状态暂不可用" : currentActivity?.content ?? (life ? "暂无进行中的活动" : "正在同步生活状态")}</div>
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
          <div className="room-art-slot"><span className="art-slot-note">生活世界场景素材待接入</span></div>
          <div className="character-crop"><img src="/assets/characters/big-fish-turnaround.jpg" alt="大肥鱼正面形象：蓝色长发与眼睛、带鲸鱼徽记的蓝白女仆装和深蓝色鱼尾。" draggable={false} /></div>
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
    <form className="explore-form" onSubmit={startExploration}><input aria-label="探索主题" value={explorationTopic} onChange={(e) => setExplorationTopic(e.target.value)} maxLength={240} placeholder="输入一个主题；留空时从已保存兴趣中选题" disabled={explorationBusy || !!exploration && ["planned", "active"].includes(exploration.status)} /><button className="button-primary" disabled={explorationBusy || !!exploration && ["planned", "active"].includes(exploration.status)}>{explorationBusy ? "安排中…" : "开始探索"}</button></form>
    <section className="autonomy-panel"><div><p className="eyebrow">BACKGROUND EXPLORATION</p><h2>后台自主探索：{autonomy ? autonomy.enabled ? "已启用" : "已关闭" : "状态未知"}</h2><p>{!autonomy ? "当前后端未返回设置状态。" : `后端运行时最多每 ${autonomy.cooldown_hours} 小时一次`}{autonomy?.paused_until && ` · 暂停至 ${new Date(autonomy.paused_until).toLocaleString()}`}</p>{autonomy?.last_skip_reason && <small>最近检查未启动：{({ no_eligible_topic: "没有符合条件的已保存兴趣、目标或待查问题", cooldown: "仍在冷却时间内", paused: "自主探索已暂停", active_task: "已有探索任务进行中" } as Record<string, string>)[autonomy.last_skip_reason] ?? "未满足启动条件"}</small>}</div><div className="autonomy-actions"><button className="button-secondary" type="button" disabled={!autonomy} onClick={() => updateAutonomy({ enabled: !autonomy?.enabled })}>{autonomy?.enabled ? "关闭自主探索" : "启用自主探索"}</button><label>探索间隔<select aria-label="自主探索间隔" disabled={!autonomy} value={autonomy?.cooldown_hours ?? 24} onChange={(event) => updateAutonomy({ cooldown_hours: Number(event.target.value) })}><option value={24}>每天最多一次</option><option value={48}>每两天一次</option><option value={168}>每周一次</option></select></label>{autonomy?.enabled && <button className="text-button" type="button" onClick={() => updateAutonomy({ paused_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() })}>暂停 24 小时</button>}{autonomy?.paused_until && <button className="text-button" type="button" onClick={() => updateAutonomy({ paused_until: null })}>恢复</button>}</div></section>
    {exploration && <article className="exploration-result" aria-live="polite"><strong>探索状态：{exploration.error_code === "insufficient_evidence" || exploration.status === "completed" && (!exploration.claims?.length || !exploration.evidence?.some((item) => item.tool_name === "read_webpage" && item.page_relevance === "relevant")) ? "资料不足，未形成可靠结论" : ({ planned: "计划中", active: "进行中", completed: "已完成", interrupted: "已中断", failed: "失败", cancelled: "已取消" } as const)[exploration.status]}</strong><span> · {exploration.selected_topic || exploration.requested_topic || "等待自主选题"} · {exploration.trigger_mode === "autonomous" ? "大肥鱼自主发起" : exploration.topic_origin === "user" ? "你指定的主题" : "根据已保存兴趣选题"}</span>{exploration.topic_basis && <span> · 依据：{({ stable_interest: "稳定兴趣", unresolved_question: "未解决问题", planned_goal: "已保存目标", derived_question: "探索衍生问题", pending_selection: "等待依据选择" } as Record<string, string>)[exploration.topic_basis] ?? exploration.topic_basis}</span>}{exploration.status === "active" && <p>当前阶段：{({ planning: "选择主题", searching: "搜索资料", reading: "安全读取网页", evaluating: "核对证据", planned: "等待开始" } as Record<string, string>)[exploration.phase ?? ""] ?? "执行中"}</p>}{exploration.timings && <small>模型 {exploration.timings.model?.count ?? 0} 次 / {Math.round((exploration.timings.model?.total_ms ?? 0) / 1000)} 秒；搜索 {Math.round((exploration.timings.search?.total_ms ?? 0) / 1000)} 秒；网页读取 {Math.round((exploration.timings.read?.total_ms ?? 0) / 1000)} 秒；总计 {Math.round((exploration.duration_ms ?? 0) / 1000)} 秒。</small>}{["planned", "active"].includes(exploration.status) && <button className="text-button" type="button" onClick={cancelExploration}>取消探索</button>}{exploration.summary && <p>{exploration.summary}</p>}{exploration.claims?.map((claim, index) => <p key={`${index}-${claim.text}`}><small>{claim.kind === "inference" ? "推断" : "来源支持"}</small> {claim.text}</p>)}{exploration.unanswered?.length > 0 && <p>还没查清：{exploration.unanswered.join("；")}</p>}{exploration.evidence?.filter((item) => item.tool_name === "read_webpage" && item.outcome === "success").map((item) => <p key={item.id}><a href={item.source_url ?? undefined} target="_blank" rel="noreferrer">{item.title || item.source_url}</a><small> · {item.retrieved_at} · {item.page_relevance === "relevant" ? "与主题相关" : item.page_relevance === "irrelevant" ? "正文无关，仅留作审计" : "旧记录未评估正文相关性"}</small></p>)}{exploration.error_code && <p className="error">{explorationFailureMessage(exploration.error_code)} 没有记作已完成探索。</p>}</article>}
    {error && <p className="error" role="alert">{error}</p>}</section></main>;

  return <main className="soulseed-app">{header}<div className="content-page chat-layout">
    <aside className="conversation-sidebar"><div className="section-kicker">SOULSEED / CHAT</div><button className="button-primary new-chat" onClick={() => newConversation().catch((e: Error) => setError(e.message))}>＋ 新会话</button><nav aria-label="会话列表">{conversations.map((item) => <button className={item.id === active?.id ? "selected" : ""} key={item.id} onClick={() => request<Conversation>(`/conversations/${item.id}`).then(setActive).catch((e: Error) => setError(e.message))}>{item.title}</button>)}</nav></aside>
    <section className="chat-panel"><header className="view-heading"><div><p className="eyebrow">A QUIET MOMENT TOGETHER</p><h1>与大肥鱼对话</h1></div><span>对话中会按需检索相关记忆</span></header>
      <div className="messages">{active?.messages?.map((message) => <article className={`message ${message.role}`} key={message.id}><label>{message.role === "user" ? "你" : "大肥鱼"}</label><p>{message.content}</p>{proactive[message.id]?.status === "delivered" && <button className="read-button" onClick={() => markRead(message.id)}>标记已读</button>}</article>)}{inbox.map((message) => <ProactivePreview key={message.id} message={message} hasConversation={!!active} onVisible={onVisible} />)}{Object.values(deliveryRetries).map(({ message, conversationId }) => <div className="error" key={message.id}>主动消息状态待同步：{message.content}<button type="button" onClick={() => void deliverAndSync(message, conversationId)}>重试投递与同步</button></div>)}{!active && inbox.length === 0 && <div className="empty">创建一个会话，开始与大肥鱼交流。</div>}</div>
      {error && <div className="error" role="alert">{error}</div>}
      {syncAfterFailure && <button className="button-secondary" type="button" disabled={busy} onClick={async () => { setBusy(true); try { const refreshed = await request<Conversation>(`/conversations/${syncAfterFailure}`); setActive(refreshed); setSyncAfterFailure(null); setError("已同步当前记录。若尚无回复，模型可能仍在处理；请勿原样重复发送。"); } catch { setError("会话同步失败，请检查网络后再次同步；不会重新发送消息。"); } finally { setBusy(false); } }}>同步会话记录</button>}
      <form className="composer" onSubmit={send}><textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder="写下你想说的话…" disabled={busy || !active} /><button className="button-primary" disabled={busy || !active || !!syncAfterFailure}>{busy ? "思考中…" : "发送"}</button></form>
    </section>
  </div></main>;

}

function LocalSession() {
  const [character, setCharacter] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const [pairing, setPairing] = useState(false);
  useEffect(() => {
    const expired = () => setCharacter(null);
    window.addEventListener("soulseed-session-expired", expired);
    request<{ character_id: string }>("/auth/session")
      .then((session) => setCharacter(session.character_id))
      .catch(() => setCharacter(null)).finally(() => setChecking(false));
    return () => window.removeEventListener("soulseed-session-expired", expired);
  }, []);
  const pair = async (event: React.FormEvent) => {
    event.preventDefault(); setPairing(true); setError("");
    try {
      const session = await request<{ character_id: string }>("/auth/session", {
        method: "POST", body: JSON.stringify({ secret }),
      });
      setCharacter(session.character_id);
    } catch (e) { setError((e as Error).message); }
    finally { setSecret(""); setPairing(false); }
  };
  if (checking) return <main className="pairing">正在确认本地会话…</main>;
  if (character) return <App key={character} characterId={character} />;
  return <main className="pairing"><h1>进入 Soulseed</h1>
    <p>输入本地配对口令，打开你的角色空间。</p>
    <form onSubmit={pair}><input aria-label="本地配对口令" type="password" autoComplete="current-password" value={secret} onChange={(e) => setSecret(e.target.value)} required />
      <button disabled={pairing}>{pairing ? "正在配对…" : "配对"}</button></form>
    {error && <p role="alert">{error}</p>}
  </main>;
}

const development = import.meta.env.VITE_SOULSEED_DEVELOPMENT_MODE === "true";
createRoot(document.getElementById("root")!).render(
  <StrictMode>{development ? <App characterId="big_fish" /> : <LocalSession />}</StrictMode>,
);
