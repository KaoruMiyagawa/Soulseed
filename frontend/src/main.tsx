import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { confirmDeliveryAndSync } from "./proactiveDelivery";
import { ApiError, createApiRequest } from "./apiRequest";

type Message = { id: string; role: "user" | "assistant" | "system"; content: string };
type Conversation = { id: string; title: string; messages?: Message[] };
type ProactiveMessage = { id: string; content: string; status: "ready" | "delivered" | "read" };
type DeliveryRetry = { message: ProactiveMessage; conversationId: string };
const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001/api";

const request = createApiRequest(API, () => window.dispatchEvent(new Event("soulseed-session-expired")));

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
  const onVisible = useCallback((id: string) => setVisibleInbox((ids) => new Set(ids).add(id)), []);

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

  return <main className="shell">
    <aside>
      <div className="brand">SOULSEED <span>V1.0-A</span></div>
      <button onClick={() => newConversation().catch((e: Error) => setError(e.message))}>＋ 新会话</button>
      <nav>{conversations.map((item) => <button className={item.id === active?.id ? "selected" : ""} key={item.id} onClick={() => request<Conversation>(`/conversations/${item.id}`).then(setActive).catch((e: Error) => setError(e.message))}>{item.title}</button>)}</nav>
    </aside>
    <section className="chat">
      <header><p>大肥鱼 · 私人空间</p><small>本地运行 · 长期记忆基础</small></header>
      <div className="messages">
        {active?.messages?.map((message) => <article className={message.role} key={message.id}>
          <label>{message.role === "user" ? "你" : "大肥鱼"}</label>
          <p>{message.content}</p>
          {proactive[message.id]?.status === "delivered" && <button className="read-button" onClick={() => markRead(message.id)}>标记已读</button>}
        </article>)}
        {inbox.map((message) => <ProactivePreview key={message.id} message={message} hasConversation={!!active} onVisible={onVisible} />)}
        {Object.values(deliveryRetries).map(({ message, conversationId }) => <div className="error" key={message.id}>
          主动消息状态待同步：{message.content}
          <button type="button" onClick={() => void deliverAndSync(message, conversationId)}>重试投递与同步</button>
        </div>)}
        {!active && inbox.length === 0 && <div className="empty">创建一个会话，开始与 Soulseed 交流。</div>}
      </div>
      {error && <div className="error">{error}</div>}
      {syncAfterFailure && <button type="button" disabled={busy} onClick={async () => {
        setBusy(true);
        try {
          const refreshed = await request<Conversation>(`/conversations/${syncAfterFailure}`);
          setActive(refreshed);
          setSyncAfterFailure(null);
          setError("已同步当前记录。若尚无回复，模型可能仍在处理；请勿原样重复发送。");
        } catch { setError("会话同步失败，请检查网络后再次同步；不会重新发送消息。"); }
        finally { setBusy(false); }
      }}>同步会话记录</button>}
      <form onSubmit={send}>
        <textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder="写下你想说的话…" disabled={busy || !active} />
        <button disabled={busy || !active || !!syncAfterFailure}>{busy ? "思考中…" : "发送"}</button>
      </form>
    </section>
  </main>;
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
