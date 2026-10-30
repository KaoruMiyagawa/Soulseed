import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Message = { id: string; role: "user" | "assistant" | "system"; content: string };
type Conversation = { id: string; title: string; messages?: Message[] };
const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8001/api";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) throw new Error((await response.json()).detail ?? `请求失败（${response.status}）`);
  return response.json();
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Conversation | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = async () => { const items = await request<Conversation[]>("/conversations"); setConversations(items); if (items[0]) setActive(await request<Conversation>(`/conversations/${items[0].id}`)); };
  useEffect(() => { load().catch((e: Error) => setError(e.message)); }, []);
  const newConversation = async () => { const created = await request<Conversation>("/conversations", { method: "POST", body: JSON.stringify({}) }); setConversations((items) => [created, ...items]); setActive({ ...created, messages: [] }); };
  const send = async (event: React.FormEvent) => { event.preventDefault(); if (!input.trim() || !active || busy) return; setBusy(true); setError(""); try { const content = input; const message = await request<Message>(`/conversations/${active.id}/messages`, { method: "POST", body: JSON.stringify({ content }) }); setActive({ ...active, messages: [...(active.messages ?? []), { id: crypto.randomUUID(), role: "user", content }, message] }); setInput(""); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  return <main className="shell"><aside><div className="brand">SOULSEED <span>V1.0-A</span></div><button onClick={() => newConversation().catch((e: Error) => setError(e.message))}>＋ 新会话</button><nav>{conversations.map((item) => <button className={item.id === active?.id ? "selected" : ""} key={item.id} onClick={() => request<Conversation>(`/conversations/${item.id}`).then(setActive)}>{item.title}</button>)}</nav></aside><section className="chat"><header><p>大肥鱼 · 私人空间</p><small>本地运行 · 长期记忆基础</small></header><div className="messages">{active?.messages?.map((message) => <article className={message.role} key={message.id}><label>{message.role === "user" ? "你" : "大肥鱼"}</label><p>{message.content}</p></article>)}{!active && <div className="empty">创建一个会话，开始与 Soulseed 交流。</div>}</div>{error && <div className="error">{error}</div>}<form onSubmit={send}><textarea value={input} onChange={(e) => setInput(e.target.value)} placeholder="写下你想说的话…" disabled={busy || !active} /><button disabled={busy || !active}>{busy ? "思考中…" : "发送"}</button></form></section></main>;
}
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);

