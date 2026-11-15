type DesktopResponse = {
  status: number;
  body: string;
  request_id: string | null;
  server_timing: string | null;
};

declare global {
  interface Window {
    __TAURI__?: { core: { invoke<T>(command: string, args: Record<string, unknown>): Promise<T> } };
  }
}

export const desktopApi = "soulseed-desktop";

export const desktopTransport: typeof fetch = async (input, init) => {
  const bridge = window.__TAURI__?.core;
  if (!bridge || typeof input !== "string" || !input.startsWith(desktopApi + "/")) {
    throw new Error("无法连接 Soulseed 桌面服务。请关闭后重新打开应用。");
  }
  init?.signal?.throwIfAborted();
  try {
    const response = await bridge.invoke<DesktopResponse>("desktop_request", {
      path: input.slice(desktopApi.length),
      method: init?.method ?? "GET",
      body: init?.body ?? null,
    });
    init?.signal?.throwIfAborted();
    const headers = new Headers({ "Content-Type": "application/json" });
    if (response.request_id) headers.set("X-Request-ID", response.request_id);
    if (response.server_timing) headers.set("Server-Timing", response.server_timing);
    return new Response(response.body, { status: response.status, headers });
  } catch (error) {
    throw new Error(typeof error === "string" ? error : "无法连接 Soulseed 桌面服务。");
  }
};
