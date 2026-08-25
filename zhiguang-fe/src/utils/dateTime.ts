const hasExplicitOffset = (value: string): boolean =>
  /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);

export const DEFAULT_BUSINESS_TIMEZONE = "Asia/Shanghai";

export const getDisplayTimezone = (timezone?: string | null): string => {
  if (timezone?.trim()) return timezone.trim();
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || DEFAULT_BUSINESS_TIMEZONE;
  } catch {
    return DEFAULT_BUSINESS_TIMEZONE;
  }
};

/** Format an API datetime instant in the resource's display timezone. */
export const formatBusinessDateTime = (
  value?: string | null,
  timezone?: string | null,
  locale = "zh-CN"
): string | undefined => {
  if (!value) return undefined;
  const raw = String(value).trim();
  if (!raw) return undefined;

  // Runtime emits offset-aware values. Keep legacy offset-less ISO values
  // deterministic at this presentation boundary instead of using the
  // browser's machine timezone.
  const parsed = new Date(
    !hasExplicitOffset(raw) && /^\d{4}-\d{2}-\d{2}T/.test(raw)
      ? `${raw}Z`
      : raw
  );
  if (Number.isNaN(parsed.getTime())) return raw;

  try {
    return new Intl.DateTimeFormat(locale, {
      timeZone: getDisplayTimezone(timezone),
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(parsed);
  } catch {
    return raw;
  }
};
