export class ApiError extends Error {
  readonly status: number;
  readonly messageSaved: boolean;
  constructor(message: string, status: number, messageSaved = false) {
    super(message);
    this.status = status;
    this.messageSaved = messageSaved;
  }
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
    const body = await response.json();
    if (isChat) timing("soulseed_chat_transport", {
      requestId: response.headers.get("X-Request-ID"), status: response.status,
      responseMs: received - started, bodyMs: performance.now() - received,
      serverTiming: response.headers.get("Server-Timing"),
    });
    if (!response.ok) {
      throw new ApiError(body.detail ?? `请求失败（${response.status}）`, response.status, body.message_saved === true);
    }
    return body;
  }

}
