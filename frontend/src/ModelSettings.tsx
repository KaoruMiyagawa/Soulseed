import { useEffect, useRef, useState, type FormEvent } from "react";
import "./modelSettings.css";

export type ModelStatus = { configured: boolean; base_url: string; model: string };
type RuntimeStatus = { day: string; daily_limit: number; used: number; remaining: number; chat_used: number;
  exploration_used: number; autonomous_paused: boolean; chat_worker_active: boolean;
  active_explorations: number; contact_available: boolean; contact_enabled: boolean; contact_used: number };
type ContactPreferences = { quiet_start_minute: number; quiet_end_minute: number };
type Request = <T>(path: string, init?: RequestInit) => Promise<T>;
export const modelPath = "/desktop/model";

export function ModelSettings({ status, request, onChange, onClose, characterId }: {
  status: ModelStatus | null;
  request: Request;
  onChange: (status: ModelStatus) => void;
  onClose: () => void;
  characterId: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [baseUrl, setBaseUrl] = useState(status?.base_url ?? "");
  const [model, setModel] = useState(status?.model ?? "");
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [dailyLimit, setDailyLimit] = useState(20);
  const [quiet, setQuiet] = useState<ContactPreferences | null>(null);
  const preferencesPath = `/characters/${encodeURIComponent(characterId)}/contact/preferences`;
  useEffect(() => {
    let live = true;
    request<RuntimeStatus>("/desktop/runtime").then((result) => {
      if (live) { setRuntime(result); setDailyLimit(result.daily_limit); }
    }).catch((error: Error) => { if (live) setError(error.message); });
    request<ContactPreferences>(preferencesPath).then((result) => {
      if (live) setQuiet(result);
    }).catch((error: Error) => { if (live) setError(error.message); });
    return () => { live = false; };
  }, [request, preferencesPath]);
  useEffect(() => {
    const element = dialog.current!;
    element.showModal();
    return () => element.close();
  }, []);

  const change = async (method: "PUT" | "DELETE") => {
    setBusy(true);
    setError("");
    try {
      const body = method === "PUT" ? JSON.stringify({ base_url: baseUrl, model, api_key: apiKey }) : undefined;
      onChange(await request<ModelStatus>(modelPath, { method, body }));
      onClose();
    } catch (error) {
      setError((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void change("PUT");
  };

  const saveRuntime = async (paused: boolean, limit = dailyLimit, contactEnabled?: boolean) => {
    setBusy(true); setError("");
    try {
      const result = await request<RuntimeStatus>("/desktop/runtime", {
        method: "PUT", body: JSON.stringify({ daily_limit: limit, autonomous_paused: paused, contact_enabled: contactEnabled }),
      });
      setRuntime(result); setDailyLimit(result.daily_limit);
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };

  const saveQuiet = async () => {
    setBusy(true); setError("");
    try {
      setQuiet(await request<ContactPreferences>(preferencesPath, { method: "POST", body: JSON.stringify({
        quiet_start_minute: quiet?.quiet_start_minute, quiet_end_minute: quiet?.quiet_end_minute,
      }) }));
    } catch (error) { setError((error as Error).message); }
    finally { setBusy(false); }
  };

  return <dialog ref={dialog} className="model-settings" aria-labelledby="model-settings-title"
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <h2 id="model-settings-title">模型设置</h2>
    <p>填写支持 OpenAI Chat Completions 的服务地址和模型 ID。发送消息时，服务商会收到生成回复所需的对话和相关记忆。</p>
    <form autoComplete="off" onSubmit={submit}>
      <label>API 基础地址
        <input required maxLength={2048} value={baseUrl} placeholder="https://你的服务商/v1"
          onChange={event => setBaseUrl(event.target.value)} />
      </label>
      <label>模型 ID
        <input required maxLength={256} value={model} onChange={event => setModel(event.target.value)} />
      </label>
      <label>API Key
        <input type="password" autoComplete="off" required maxLength={4096} value={apiKey}
          onChange={event => setApiKey(event.target.value)} />
      </label>
      <p>保存配置时不会连接服务商。API Key 由 Windows 为当前用户加密。</p>
      <section className="desktop-runtime-status" aria-label="私人内测运行状态">
        <strong>私人内测运行状态</strong>
        {runtime ? <>
          <p>今日模型请求 {runtime.used}/{runtime.daily_limit}，剩余 {runtime.remaining} 次。聊天 {runtime.chat_used} 次，探索 {runtime.exploration_used} 次，主动联系 {runtime.contact_used} 次。按 UTC 日重置。</p>
          <p>异步回复 Worker：{runtime.chat_worker_active ? "运行中" : "未运行"} · 探索任务：{runtime.active_explorations} 个 · 主动联系：{runtime.contact_enabled ? runtime.autonomous_paused ? "已暂停" : "已启用" : "已关闭"}</p>
          <label>每日模型请求上限（1–20 次）
            <input type="number" min={1} max={20} value={dailyLimit} disabled={busy}
              onChange={event => setDailyLimit(Number(event.target.value))} />
          </label>
          <div className="desktop-runtime-actions">
            <button type="button" className="button-secondary" disabled={busy || !Number.isInteger(dailyLimit) || dailyLimit < 1 || dailyLimit > 20}
              onClick={() => void saveRuntime(runtime.autonomous_paused)}>保存每日额度</button>
            <button type="button" className="button-secondary" disabled={busy}
              onClick={() => void saveRuntime(!runtime.autonomous_paused, runtime.daily_limit)}>
              {runtime.autonomous_paused ? "恢复自主功能" : "一键暂停自主功能"}
            </button>
            <button type="button" className="button-secondary" disabled={busy || !runtime.contact_available}
              onClick={() => void saveRuntime(runtime.autonomous_paused, runtime.daily_limit, !runtime.contact_enabled)}>
              {runtime.contact_enabled ? "关闭主动联系" : "启用主动联系"}
            </button>
          </div>
          {quiet && <div className="desktop-runtime-actions">
            {(["quiet_start_minute", "quiet_end_minute"] as const).map((key, index) => {
              const minutes = quiet[key];
              const value = `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
              return <label key={key}>{index === 0 ? "安静时段开始" : "安静时段结束"}
                <input type="time" value={value} disabled={busy} onChange={event => {
                  if (!event.target.value) return;
                  const [hour, minute] = event.target.value.split(":").map(Number);
                  setQuiet({ ...quiet, [key]: hour * 60 + minute });
                }} />
              </label>;
            })}
            <button type="button" className="button-secondary" disabled={busy} onClick={() => void saveQuiet()}>保存安静时段</button>
          </div>}
          <p>主动联系与自主探索只在 Soulseed 运行时工作。窗口不在前台时，Windows 通知设置决定是否显示提醒，消息始终保留在聊天中。</p>
        </> : <p>正在读取桌面运行状态…</p>}
      </section>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="model-settings-actions">
        {(status === null || status.configured) && <button type="button" className="text-button" disabled={busy}
          onClick={() => void change("DELETE")}>清除配置</button>}
        <button type="button" className="button-secondary" disabled={busy} onClick={onClose}>取消</button>
        <button className="button-primary" disabled={busy}>{busy ? "处理中…" : "保存"}</button>
      </div>
    </form>
  </dialog>;
}
