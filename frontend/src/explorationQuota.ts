export type ExplorationQuota = {
  limit: number; used: number | null; remaining: number | null;
  available: boolean; reason: string | null; resets_at: string; day: string;
};

export function quotaAllowsStart(quota: ExplorationQuota | null): boolean {
  return quota?.available === true && quota.remaining !== null && quota.remaining > 0;
}

export function quotaMessage(quota: ExplorationQuota | null, timeZone?: string): string {
  if (!quota || quota.used === null || quota.remaining === null) {
    return "探索额度暂时无法核验，已暂停新探索；仍可查看已有结果。";
  }
  const localReset = new Intl.DateTimeFormat(undefined, {
    dateStyle: "short", timeStyle: "long", ...(timeZone ? { timeZone } : {}),
  }).format(new Date(quota.resets_at));
  return `今日探索请求预留 ${quota.used} / ${quota.limit}，剩余 ${quota.remaining} 次。${quota.remaining === 0 ? "今日额度已用完。" : ""}下次重置：${localReset}（UTC ${quota.resets_at.slice(0, 16).replace("T", " ")}）。`;
}
