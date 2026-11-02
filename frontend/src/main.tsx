import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { confirmDeliveryAndSync } from "./proactiveDelivery";

type Message = { id: string; role: "user" | "assistant" | "system"; content: string };
type Conversation = { id: string; title: string; messages?: Message[] };
type ProactiveMessage = { id: string; content: string; status: "ready" | "delivered" | "read" };
type DeliveryRetry = { message: ProactiveMessage; conversationId: string };
const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001/api";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { credentials: "include", headers: { "Content-Type": "application/json" }, ...options });
  if (response.status === 401) window.dispatchEvent(new Event("soulseed-session-expired"));
  if (!response.ok) throw new Error((await response.json()).detail ?? `请求失败（${response.status}）`);
  return response.json();
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
    if (!input.trim() || !active || busy) return;
    setBusy(true);
    setError("");
    try {
      const content = input;
      const conversationId = active.id;
      const message = await request<Message>(`/conversations/${conversationId}/messages`, {
        method: "POST", body: JSON.stringify({ content }),
      });
      setActive((conversation) => conversation?.id === conversationId
        ? { ...conversation, messages: [...(conversation.messages ?? []), { id: crypto.randomUUID(), role: "user", content }, message] }
        : conversation);
      setInput("");
    } catch (e) {
      setError((e as Error).message);
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
      <button onClick={() => request("/auth/logout", { method: "POST" }).then(() => window.dispatchEvent(new Event("soulseed-session-expired"))).catch((e: Error) => setError(e.message))}>退出本地会话</button>
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
      <form onSubmit={send}>
        <textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder="写下你想说的话…" disabled={busy || !active} />
        <button disabled={busy || !active}>{busy ? "思考中…" : "发送"}</button>
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

createRoot(document.getElementById("root")!).render(<StrictMode><LocalSession /></StrictMode>);
