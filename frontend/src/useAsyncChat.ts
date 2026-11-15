import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChatQueue, matchesQueuedMessage, mergeMessages, submissionMessage, type ChatMessage, type ChatRequest,
  type QueueEntry, type Submission } from "./chatQueue";

type Feed = { messages: ChatMessage[]; next_after_sequence: number; has_more: boolean; async_chat_accepting: boolean };
type ReplyMode = "mock" | "real" | "disabled" | "unknown";
const timeout = (signal: AbortSignal) => ({ signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]) });

/** Run one request cycle at a time, with event-driven wakeup and no background polling in hidden tabs. */
function watch(work: (signal: AbortSignal) => Promise<number>, onError: () => void, localTransport: boolean) {
  let timer: number | undefined;
  let active: AbortController | undefined;
  let stopped = false;
  let again = false;
  const run = async () => {
    if (stopped || document.hidden || !localTransport && !navigator.onLine) return;
    const request = new AbortController();
    active = request;
    let delay = 3000;
    try {
      delay = await work(request.signal);
    } catch {
      if (!request.signal.aborted) onError();
    } finally {
      active = undefined;
      if (!stopped && !document.hidden) {
        timer = window.setTimeout(run, again ? 0 : delay);
        again = false;
      }
    }
  };
  const wake = () => {
    window.clearTimeout(timer);
    if (document.hidden || !localTransport && !navigator.onLine) {
      active?.abort();
    } else if (active) {
      again = true;
    } else if (!stopped) {
      timer = window.setTimeout(run, 0);
    }
  };
  document.addEventListener("visibilitychange", wake);
  window.addEventListener("online", wake);
  window.addEventListener("offline", wake);
  wake();
  return { wake, stop: () => {
    stopped = true;
    window.clearTimeout(timer);
    active?.abort();
    document.removeEventListener("visibilitychange", wake);
    window.removeEventListener("online", wake);
    window.removeEventListener("offline", wake);
  } };
}

export function useAsyncChat(api: string, user: string, character: string, conversation: string, request: ChatRequest,
  localTransport = false) {
  const queue = useMemo(() => new ChatQueue(api, user, character, conversation), [api, user, character, conversation]);
  const [pending, setPending] = useState<QueueEntry[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [entering, setEntering] = useState<ReadonlySet<string>>(new Set());
  const [loaded, setLoaded] = useState(false);
  const [online, setOnline] = useState(localTransport || navigator.onLine);
  const [mode, setMode] = useState<ReplyMode>("unknown");
  const [accepting, setAccepting] = useState(false);
  const [queueError, setQueueError] = useState("");
  const [readError, setReadError] = useState("");
  const feed = useRef({ cursor: 0, hydrated: false, messages: [] as ChatMessage[] });
  const sendWake = useRef(() => {});
  const receiveWake = useRef(() => {});
  const live = useRef(false);

  const refreshQueue = useCallback(() => {
    if (!live.current) return;
    try {
      const snapshot = queue.snapshot();
      const ids = new Set(snapshot.map(item => item.client_message_id));
      const saved = new Map(feed.current.messages.filter(item => item.client_message_id)
        .map(item => [item.client_message_id, item]));
      // A different tab can dequeue an acknowledged entry before our feed reaches it.
      setPending(previous => [
        ...previous.filter(item => !ids.has(item.client_message_id) && !matchesQueuedMessage(item, saved.get(item.client_message_id))),
        ...snapshot,
      ]);
      setQueueError("");
    } catch {
      setQueueError("本地发送队列无法读取，请保留浏览器中的会话数据。");
    }
  }, [queue]);

  const receive = useCallback((incoming: ChatMessage[], animate: boolean, fromSave = false) => {
    if (!live.current || !incoming.length) return;
    const current = feed.current;
    if (animate) {
      const known = new Set(current.messages.map(item => item.id));
      const newReplies = incoming.filter(item => item.role === "assistant" && item.user_message_id && !known.has(item.id));
      if (newReplies.length) setEntering(previous => new Set([...previous, ...newReplies.map(item => item.id)]));
    }
    current.messages = mergeMessages(current.messages, incoming, fromSave);
    setMessages(current.messages);
    refreshQueue();
  }, [refreshQueue]);

  useEffect(() => {
    live.current = true;
    let admissionOpen = false;
    refreshQueue();
    const updateConnection = () => setOnline(localTransport || navigator.onLine);
    const send = watch(async signal => {
      const sent = await queue.sendHead(request, signal, refreshQueue, record => {
        receive([submissionMessage(record), ...(record.reply ? [record.reply] : [])], feed.current.hydrated, true);
        receiveWake.current();
      }, () => admissionOpen);
      const head = queue.snapshot()[0];
      return sent ? 0 : admissionOpen && head && !head.failure?.permanent
        ? Math.max(350, (head.failure?.retryAfter ?? 0) - Date.now()) : 4000;
    }, () => setQueueError("暂时无法处理本地发送队列。"), localTransport);
    sendWake.current = send.wake;

    let statusIndex = 0;
    let nextHealth = 0;
    let replyMode: ReplyMode = "unknown";
    const read = watch(async signal => {
      if (Date.now() >= nextHealth) {
        try {
          const health = await request<{ chat_reply_mode?: ReplyMode }>("/health", timeout(signal));
          if (!signal.aborted) {
            replyMode = health.chat_reply_mode === "mock" || health.chat_reply_mode === "real" || health.chat_reply_mode === "disabled" ? health.chat_reply_mode : "unknown";
            setMode(replyMode);
          }
        } catch {
          if (!signal.aborted) {
            replyMode = "unknown";
            setMode(replyMode);
          }
        }
        nextHealth = Date.now() + 15000;
      }
      signal.throwIfAborted();
      const current = feed.current;
      const base = `/conversations/${encodeURIComponent(conversation)}`;
      const page = await request<Feed>(`${base}/messages?after_sequence=${current.cursor}&limit=100`, timeout(signal));
      signal.throwIfAborted();
      receive(page.messages, current.hydrated);
      current.cursor = page.next_after_sequence;
      if (admissionOpen !== (page.async_chat_accepting === true)) {
        admissionOpen = page.async_chat_accepting === true;
        setAccepting(admissionOpen);
        if (admissionOpen) send.wake();
      }
      if (page.has_more) return 0;
      current.hydrated = true;
      setLoaded(true);
      setReadError("");

      const waiting = current.messages.filter(item => item.client_message_id &&
        (item.role_status === "waiting" || item.role_status === "processing" || item.role_status === "unknown"));
      // A disabled worker can still have a just-committed reply or a reconciled status to read.
      if (waiting.length) {
        const batch = Array.from({ length: Math.min(8, waiting.length) }, (_, index) =>
          waiting[(statusIndex + index) % waiting.length]);
        statusIndex += batch.length;
        const statuses = await Promise.allSettled(batch.map(item => request<Submission>(
          `${base}/user-messages/${encodeURIComponent(item.client_message_id!)}`, timeout(signal),
        )));
        signal.throwIfAborted();
        for (const result of statuses) {
          if (result.status === "fulfilled") {
            const record = result.value;
            receive([submissionMessage(record), ...(record.reply ? [record.reply] : [])], true);
          }
        }
        if (statuses.some(result => result.status === "rejected")) {
          setReadError("部分回复状态暂时无法同步，稍后会继续查询。");
          return 3000;
        }
        return replyMode === "disabled" ? 5000 : 700;
      }
      return 2500;
    }, () => setReadError("聊天记录暂时无法同步，稍后会继续查询。"), localTransport);
    receiveWake.current = read.wake;
    const onStorage = (event: StorageEvent) => {
      if (event.key !== queue.key) return;
      refreshQueue();
      send.wake();
      read.wake();
    };
    window.addEventListener("storage", onStorage);
    window.addEventListener("online", updateConnection);
    window.addEventListener("offline", updateConnection);
    return () => {
      live.current = false;
      send.stop();
      read.stop();
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("online", updateConnection);
      window.removeEventListener("offline", updateConnection);
    };
  }, [conversation, queue, receive, refreshQueue, request, localTransport]);

  const enqueue = async (content: string) => {
    const entry = await queue.enqueue(content);
    refreshQueue();
    sendWake.current();
    return entry;
  };
  const retry = async (id: string) => {
    try {
      await queue.retry(id);
      refreshQueue();
      sendWake.current();
    } catch {
      if (live.current) setQueueError("暂时无法读取发送队列，请稍后再试。");
    }
  };

  return { messages, entering, pending, online, mode, accepting, loaded, queueError, readError, enqueue, retry,
    refresh: () => receiveWake.current() };
}
