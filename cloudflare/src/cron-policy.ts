export interface CronScheduleInput {
  kind?: string;
  at?: string;
  afterSeconds?: number;
  everySeconds?: number;
  startsAt?: string;
}

export interface ParsedCronSchedule {
  kind: "once" | "interval";
  scheduleAt: number;
  everySeconds: number | null;
}

export function parseCronSchedule(schedule: CronScheduleInput, now = Date.now()): ParsedCronSchedule {
  if (schedule.kind === "delay") {
    const delay = Number(schedule.afterSeconds);
    if (!Number.isFinite(delay) || delay <= 0 || delay > 31_536_000) throw new Error("cron_invalid_schedule");
    return { kind: "once", scheduleAt: now + delay * 1000, everySeconds: null };
  }
  if (schedule.kind === "once") {
    const scheduleAt = typeof schedule.at === "string" ? Date.parse(schedule.at) : Number.NaN;
    if (!Number.isFinite(scheduleAt)) throw new Error("cron_invalid_schedule");
    return { kind: "once", scheduleAt, everySeconds: null };
  }
  if (schedule.kind === "interval") {
    const everySeconds = Number(schedule.everySeconds);
    if (!Number.isFinite(everySeconds) || everySeconds <= 0) throw new Error("cron_invalid_schedule");
    const startsAt = typeof schedule.startsAt === "string" ? Date.parse(schedule.startsAt) : now + everySeconds * 1000;
    if (!Number.isFinite(startsAt)) throw new Error("cron_invalid_schedule");
    return { kind: "interval", scheduleAt: startsAt, everySeconds };
  }
  throw new Error(schedule.kind === "cron" ? "cron_expression_unsupported" : "cron_invalid_schedule");
}

export function nextIntervalAt(scheduledAt: number, everySeconds: number, now = Date.now()): number {
  const interval = everySeconds * 1000;
  if (!Number.isFinite(scheduledAt) || !Number.isFinite(interval) || interval <= 0) throw new Error("cron_invalid_schedule");
  const elapsed = Math.max(0, now - scheduledAt);
  return scheduledAt + (Math.floor(elapsed / interval) + 1) * interval;
}
