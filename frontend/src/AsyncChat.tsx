import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { ChatAvatar } from "./ChatAvatar";
import { ApiError } from "./apiRequest";
import { ChatTranscript, type SendMotion } from "./ChatTranscript";
import { matchesQueuedMessage, messageKey, type ChatRequest, type RoleStatus } from "./chatQueue";
import { useAsyncChat } from "./useAsyncChat";
import "./asyncChat.css";

const statusText: Record<RoleStatus, string> = {
  waiting: "已保存 · 等待角色回复",
  processing: "已保存 · 角色处理中",
  completed: "回复完成",
  failed: "已保存 · 回复失败",
  unknown: "已保存 · 回复结果未知",
};

function MessageRow({ id, identity, role, content, source, status, statusError, enter, retry, markRead }: {
  id?: string; identity: string; role: "assistant" | "user" | "system"; content: string; source?: string | null;
  status?: string; statusError?: boolean; enter?: boolean; retry?: () => void; markRead?: () => void;
}) {
  return <article className={`message ${role}${enter ? " chat-reply-enter" : ""}`}
    data-chat-key={identity} data-message-id={id} data-reply-to={source ?? undefined}>
    {role === "assistant" ? <ChatAvatar /> : role === "user" ? <span className="user-avatar" aria-hidden="true">
      <svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="3" /><path d="M6 20v-2a6 6 0 0 1 12 0v2" /></svg>
    </span> : null}
    <div className="message-body">
      <label>{role === "user" ? "你" : role === "system" ? "系统消息" : "大肥鱼"}</label>
      <p className="chat-bubble">{content}</p>
      {status && <small className="chat-message-status" data-error={statusError || undefined}>
        <span role="status">{status}</span>{retry && <button type="button" onClick={retry}>重试发送</button>}
      </small>}
      {markRead && <button className="read-button" onClick={markRead}>标记已读</button>}
    </div>
  </article>;
}

export function ChatEmpty({ hasConversation }: { hasConversation: boolean }) {
  return <div className="chat-empty"><ChatAvatar /><h2>从一句话开始</h2>
    <p>{hasConversation ? "写下此刻想分享的事。" : "创建一个会话，开始与大肥鱼交流。"}</p></div>;
}

export function AsyncChat({ api, user, character, conversation, request, children, extraCount, error, isDelivered, markRead, desktopModelReady }: {
  api: string; user: string; character: string; conversation: string; request: ChatRequest;
  children: ReactNode; extraCount: number; error: string; isDelivered: (id: string) => boolean; markRead: (id: string) => void;
  desktopModelReady?: boolean;
}) {
  const chat = useAsyncChat(api, user, character, conversation, request, desktopModelReady !== undefined);
  const [input, setInput] = useState("");
  const [sendError, setSendError] = useState("");
  const [motion, setMotion] = useState<SendMotion>();
  const [syncMode, setSyncMode] = useState(desktopModelReady !== undefined);
  const [syncSending, setSyncSending] = useState(false);
  const composer = useRef<HTMLFormElement>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);
  const enqueuing = useRef(false);
  const draft = useRef("");
  const unresolved = chat.messages.some(message =>
    message.role_status === "waiting" || message.role_status === "processing" || message.role_status === "unknown");
  const syncAvailable = chat.loaded && !chat.accepting && !unresolved && chat.pending.length === 0;
  const canSend = desktopModelReady !== false && (chat.accepting || syncAvailable && syncMode);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const original = draft.current;
    const content = original.trim();
    if (!canSend || !content || composing.current || enqueuing.current) return;
    enqueuing.current = true;
    setSendError("");
    const origin = textarea.current!.getBoundingClientRect();
    const clearDraft = () => {
      if (draft.current !== original) return;
      draft.current = "";
      setInput("");
    };
    try {
      if (!chat.accepting) {
        setSyncSending(true);
        try {
          await request(`/conversations/${encodeURIComponent(conversation)}/messages`, {
            method: "POST", body: JSON.stringify({ content }),
          });
          clearDraft();
        } catch (error) {
          const savedOrUncertain = !(error instanceof ApiError) || error.messageSaved;
          if (savedOrUncertain) clearDraft();
          setSendError(savedOrUncertain && (desktopModelReady === undefined || !(error instanceof ApiError))
            ? "同步回复未确认，请先查看聊天记录。" : (error as Error).message);
        } finally {
          setSyncSending(false);
          chat.refresh();
          textarea.current?.focus({ preventScroll: true });
        }
        return;
      }
      const item = await chat.enqueue(content);
      clearDraft();
      setMotion({ key: `client:${item.client_message_id}`, origin });
      textarea.current?.focus({ preventScroll: true });
    } catch {
      setSendError("无法在此浏览器保存发送队列，草稿仍在输入框中。");
    } finally {
      enqueuing.current = false;
    }
  };

  const saved = new Map(chat.messages.filter(message => message.client_message_id)
    .map(message => [message.client_message_id, message]));
  const pending = chat.pending.filter(item => !matchesQueuedMessage(item, saved.get(item.client_message_id)));
  const notices = [sendError, chat.queueError, chat.readError, error].filter(Boolean);
  return <>
    <ChatTranscript conversationId={conversation} messageCount={chat.messages.length + pending.length + extraCount} motion={motion}>
      {[...chat.messages.map(message => <MessageRow key={messageKey(message)} identity={messageKey(message)}
        id={message.id} role={message.role} content={message.content} source={message.user_message_id}
        status={message.role_status ? statusText[message.role_status] : undefined}
        statusError={message.role_status === "failed" || message.role_status === "unknown"}
        enter={chat.entering.has(message.id)}
        markRead={isDelivered(message.id) ? () => markRead(message.id) : undefined} />),
      ...pending.map(item => {
        const identity = saved.has(item.client_message_id) ? `conflict:${item.client_message_id}` : `client:${item.client_message_id}`;
        const head = chat.pending[0];
        const status = item.failure ? "发送失败 · 未确认保存"
          : !chat.online ? "等待联网 · 未确认保存"
          : head?.client_message_id !== item.client_message_id ? "排队中 · 等待前一条确认"
          : "发送中 · 未确认保存";
        return <MessageRow key={identity} identity={identity} role="user" content={item.content} status={status}
          statusError={!!item.failure} retry={item.failure ? () => void chat.retry(item.client_message_id) : undefined} />;
      })]}
      {children}
      {chat.loaded && !chat.messages.length && !pending.length && !extraCount && <ChatEmpty hasConversation />}
    </ChatTranscript>
    <div className="chat-compose-area">
      <small className="chat-reply-mode" role="status">{desktopModelReady === false ? "请先在「模型设置」保存你自己的模型配置。"
        : desktopModelReady === true && syncAvailable ? "同步聊天 · 异步回复未开放"
        : chat.accepting && chat.mode === "mock" ? "隔离测试 · Mock 回复"
        : chat.accepting ? "异步回复可用"
        : syncAvailable && syncMode ? "同步聊天 · 发送后等待角色回复"
        : chat.loaded ? "异步回复当前不可用。" : "正在确认异步回复服务状态…"}</small>
      {syncAvailable && !syncMode && <button className="text-button" type="button" onClick={() => setSyncMode(true)}>使用同步聊天</button>}
      {notices.length > 0 && <div className="error chat-notice" role="alert">{notices.join(" ")}</div>}
      <form className="composer" ref={composer} onSubmit={send}>
        <label className="chat-sr-only" htmlFor="chat-input">写给大肥鱼的消息</label>
        <textarea id="chat-input" ref={textarea} aria-describedby="chat-input-help" rows={2} value={input}
          maxLength={20000} placeholder="写下你想说的话…" onChange={event => {
            draft.current = event.target.value;
            setInput(draft.current);
          }}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={event => {
            if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey) || event.repeat
              || composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
            event.preventDefault();
            composer.current?.requestSubmit();
          }} />
        <div className="composer-footer"><span id="chat-input-help">Enter 换行 · Ctrl / ⌘ + Enter 发送</span>
          <button className="button-primary" type="submit" disabled={!canSend || syncSending || !input.trim()}>
            {syncSending ? "等待回复…" : "发送"}
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 11 16-7-7 16-2-7-7-2ZM11 13l9-9" /></svg>
          </button>
        </div>
      </form>
    </div>
  </>;
}
