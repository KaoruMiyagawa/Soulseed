export type ExplorationQuota = {
  limit: number; used: number | null; remaining: number | null;
  available: boolean; reason: string | null; resets_at?: string | null; day?: string;
};

export function quotaAllowsStart(quota: ExplorationQuota | null): boolean {
  return quota?.available === true && quota.remaining !== null && quota.remaining > 0;
}

export function quotaMessage(quota: ExplorationQuota | null, timeZone?: string): string {
  if (!quota || quota.used === null || quota.remaining === null) {
    return "探索额度暂时无法核验，已暂停新探索；仍可查看已有结果。";
  }
  const reset = typeof quota.resets_at === "string" ? new Date(quota.resets_at) : null;
  if (!reset || !Number.isFinite(reset.getTime())) {
    return `今日探索请求预留 ${quota.used} / ${quota.limit}，剩余 ${quota.remaining} 次。重置时间暂不可用。`;
  }
  const localReset = new Intl.DateTimeFormat(undefined, {
    dateStyle: "short", timeStyle: "long", ...(timeZone ? { timeZone } : {}),
  }).format(reset);
  return `今日探索请求预留 ${quota.used} / ${quota.limit}，剩余 ${quota.remaining} 次。${quota.remaining === 0 ? "今日额度已用完。" : ""}下次重置：${localReset}（UTC ${reset.toISOString().slice(0, 16).replace("T", " ")}）。`;
}
