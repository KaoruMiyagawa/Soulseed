export class ApiError extends Error {
  readonly status: number;
  readonly messageSaved: boolean;
  constructor(message: string, status: number, messageSaved = false) {
    super(message);
    this.status = status;
    this.messageSaved = messageSaved;
  }
}

/** Convert API error payloads into short, safe text for the UI. */
export function apiErrorMessage(payload: unknown, status: number): string {
  const readText = (value: unknown): string | null => {
    if (typeof value === "string" && value.trim()) return value.trim().slice(0, 300);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const record = value as Record<string, unknown>;
    for (const key of ["message", "detail", "msg"]) {
      if (typeof record[key] === "string" && (record[key] as string).trim()) {
        return (record[key] as string).trim().slice(0, 300);
      }
    }
    return null;
  };
  const record = payload && typeof payload === "object" && !Array.isArray(payload)
    ? payload as Record<string, unknown> : null;
  const candidates = [readText(record?.detail), readText(record?.message)];
  const message = candidates.find((candidate) => candidate !== null);
  if (message) return message;
  if (Array.isArray(record?.detail)) {
    const validationMessages = record.detail
      .map((item) => item && typeof item === "object" && !Array.isArray(item)
        ? (item as Record<string, unknown>).msg : null)
      .filter((item): item is string => typeof item === "string" && !!item.trim())
      .slice(0, 2);
    if (validationMessages.length) return validationMessages.join("；").slice(0, 300);
  }
  return `请求失败（${status}）`;
}

export function createApiRequest(API: string, onUnauthorized: () => void,
  timing: (event: string, record: Record<string, unknown>) => void = console.info) {
  return async function request<T>(path: string, options?: RequestInit): Promise<T> {
    const started = performance.now();
    const isChat = options?.method === "POST" && /^\/conversations\/[^/]+\/messages$/.test(path);
    let response: Response;
    try {
      response = await fetch(`${API}${path}`, { credentials: "include", headers: { "Content-Type": "application/json" }, ...options });
    } catch {
      if (isChat) timing("soulseed_chat_transport", { outcome: "network_failure", elapsedMs: performance.now() - started });
      throw new Error("网络连接中断，无法确认服务端是否已处理请求；系统没有自动重发。");
    }
    if (response.status === 401) onUnauthorized();
    const received = performance.now();
    let body: unknown;
    try { body = await response.json(); } catch { body = null; }
    if (isChat) timing("soulseed_chat_transport", {
      requestId: response.headers.get("X-Request-ID"), status: response.status,
      responseMs: received - started, bodyMs: performance.now() - received,
      serverTiming: response.headers.get("Server-Timing"),
    });
    if (!response.ok) {
      const record = body && typeof body === "object" && !Array.isArray(body)
        ? body as Record<string, unknown> : null;
      throw new ApiError(apiErrorMessage(body, response.status), response.status, record?.message_saved === true);
    }
    return body as T;
  }

}
