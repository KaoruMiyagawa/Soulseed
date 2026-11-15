export type RoleStatus = "waiting" | "processing" | "completed" | "failed" | "unknown";
export type ChatMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  server_sequence: number;
  user_message_id?: string | null;
  client_message_id?: string | null;
  client_stream_id?: string | null;
  client_sequence?: number | null;
  role_status?: RoleStatus | null;
  reply_error_code?: string | null;
};
export type Submission = Omit<ChatMessage, "id"> & {
  message_id: string;
  conversation_id: string;
  client_message_id: string;
  client_stream_id: string;
  client_sequence: number;
  save_status: "saved";
  reply?: ChatMessage | null;
};
export type QueueEntry = {
  client_message_id: string;
  client_stream_id: string;
  client_sequence: number;
  content: string;
  attempted: boolean;
  failure?: { permanent: boolean; retryAfter: number; attempts: number };
};
type Outbox = { stream: string; next: number; pending: QueueEntry[] };
export type ChatRequest = <T>(path: string, options?: RequestInit) => Promise<T>;

function httpStatus(error: unknown): number | undefined {
  return typeof error === "object" && error !== null && "status" in error && typeof error.status === "number"
    ? error.status : undefined;
}

export const messageKey = (message: ChatMessage) =>
  message.role === "user" && message.client_message_id ? `client:${message.client_message_id}` : `server:${message.id}`;

export function matchesQueuedMessage(entry: QueueEntry, saved: ChatMessage | undefined): boolean {
  return !!saved && saved.role === "user" && saved.client_message_id === entry.client_message_id
    && saved.client_stream_id === entry.client_stream_id && saved.client_sequence === entry.client_sequence
    && saved.content === entry.content;
}

export function submissionMessage(record: Submission): ChatMessage {
  return { ...record, id: record.message_id };
}

export function mergeMessages(previous: ChatMessage[], incoming: ChatMessage[], fromSave = false): ChatMessage[] {
  const records = new Map(previous.map(message => [message.id, message]));
  for (const item of incoming) {
    const prior = records.get(item.id);
    const status = prior?.role_status;
    // A delayed POST/feed must not replace a newer status already read by the client.
    if (status && (["completed", "failed"].includes(status)
      || fromSave && (status === "unknown" || status === "processing" && item.role_status === "waiting"))) {
      records.set(item.id, { ...item, role_status: status, reply_error_code: prior.reply_error_code });
    } else {
      records.set(item.id, { ...prior, ...item });
    }
  }
  return [...records.values()].sort((a, b) => a.server_sequence - b.server_sequence);
}

export class ChatQueue {
  readonly key: string;
  readonly endpoint: string;
  private readonly conversation: string;

  constructor(api: string, user: string, character: string, conversation: string) {
    this.conversation = conversation;
    this.key = "soulseed-chat-outbox-v1:" + JSON.stringify([api, user, character, conversation]);
    this.endpoint = `/conversations/${encodeURIComponent(conversation)}/user-messages`;
  }

  private read(): Outbox | null {
    const saved = localStorage.getItem(this.key);
    if (saved === null) return null;
    const box = JSON.parse(saved) as Outbox;
    if (!box || !box.stream || !Number.isSafeInteger(box.next) || box.next < 1 || !Array.isArray(box.pending)
      || box.pending.some((item, index) => !item.client_message_id || item.client_stream_id !== box.stream
        || item.client_sequence !== box.next - box.pending.length + index || typeof item.content !== "string")) {
      throw new Error("本地发送队列无法读取");
    }
    return box;
  }

  snapshot(): QueueEntry[] {
    return this.read()?.pending ?? [];
  }

  private update<T>(change: (box: Outbox) => T): Promise<T> {
    return navigator.locks.request(this.key, () => {
      const box = this.read() ?? { stream: crypto.randomUUID(), next: 1, pending: [] };
      const result = change(box);
      localStorage.setItem(this.key, JSON.stringify(box));
      return result;
    });
  }

  enqueue(content: string): Promise<QueueEntry> {
    return this.update(box => {
      const item: QueueEntry = {
        client_message_id: crypto.randomUUID(), client_stream_id: box.stream, client_sequence: box.next++,
        content, attempted: false,
      };
      box.pending.push(item);
      return item;
    });
  }

  retry(id: string): Promise<void> {
    return this.update(box => {
      if (box.pending[0]?.client_message_id === id) delete box.pending[0].failure;
    });
  }

  async sendHead(request: ChatRequest, signal: AbortSignal, changed: () => void,
                 confirmed: (record: Submission) => void, canPost = () => true): Promise<boolean> {
    if (signal.aborted || !this.snapshot().length) return false;
    // Separate locks let another tab enqueue while a save acknowledgement is outstanding.
    return navigator.locks.request(this.key + ":send", { ifAvailable: true }, async lock => {
      if (!lock || signal.aborted) return false;
      const first = this.snapshot()[0];
      if (!first) return false;
      if (canPost() ? first.failure?.permanent || (first.failure?.retryAfter ?? 0) > Date.now() : !first.attempted) {
        return false;
      }
      const { client_message_id, client_stream_id, client_sequence, content } = first;
      const statusPath = `${this.endpoint}/${encodeURIComponent(client_message_id)}`;
      const options = () => ({ signal: AbortSignal.any([signal, AbortSignal.timeout(8000)]) });
      const lookup = async () => {
        try {
          return await request<Submission>(statusPath, options());
        } catch (error) {
          if (httpStatus(error) !== 404) throw error;
          return null;
        }
      };
      const updateHead = (change: (box: Outbox, head: QueueEntry) => void) => this.update(box => {
        const head = box.pending[0];
        if (head?.client_message_id === client_message_id) change(box, head);
      });
      try {
        let record = first.attempted ? await lookup() : null;
        if (!record) {
          if (!canPost()) return false;
          // Persist uncertainty before issuing POST so navigation or a crash always resumes with GET.
          await updateHead((_box, head) => { head.attempted = true; delete head.failure; });
          signal.throwIfAborted();
          if (!canPost()) return false;
          changed();
          try {
            record = await request<Submission>(this.endpoint, {
              ...options(), method: "POST", body: JSON.stringify({ client_message_id, client_stream_id, client_sequence, content }),
            });
          } catch (error) {
            const status = httpStatus(error);
            if (signal.aborted || status && status < 500 && status !== 408) throw error;
            record = await lookup();
            if (!record) throw error;
          }
        }
        if (record.save_status !== "saved" || record.conversation_id !== this.conversation
          || record.client_message_id !== client_message_id || record.client_stream_id !== client_stream_id
          || record.client_sequence !== client_sequence || record.content !== content || record.role !== "user"
          || !record.message_id || !Number.isSafeInteger(record.server_sequence) || record.server_sequence < 1) {
          throw Object.assign(new Error("保存确认与原消息不符"), { status: 409 });
        }
        // Merge before dequeuing so the optimistic node survives the handoff to the server row.
        if (!signal.aborted) confirmed(record);
        await updateHead(box => { box.pending.shift(); });
        if (!signal.aborted) changed();
        return true;
      } catch (error) {
        if (signal.aborted || !canPost()) return false;
        const status = httpStatus(error);
        const permanent = !!status && status >= 400 && status < 500 && ![401, 408, 429].includes(status);
        const attempts = (first.failure?.attempts ?? 0) + 1;
        await updateHead((_box, head) => {
          head.failure = { permanent, attempts, retryAfter: Date.now() + Math.min(30000, 1000 * 2 ** attempts) };
        });
        changed();
        return false;
      }
    });
  }
}
