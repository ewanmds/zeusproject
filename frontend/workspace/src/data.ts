import type { Snapshot } from "./types";
export class APIError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
let csrf = "";
let authGeneration = 0;
export function invalidateIdentity() {
  authGeneration++;
}
export function setCSRF(value: string) {
  if (value !== csrf) authGeneration++;
  csrf = value;
}
export async function api<T>(
  path: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const generation = authGeneration;
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    body === undefined ? 12000 : 45000,
  );
  try {
    const response = await fetch("/api/v2" + path, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        ...(body === undefined
          ? {}
          : { "Content-Type": "application/json", "X-Zeus-CSRF": csrf }),
        ...headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response.json();
    if (path !== "/config" && generation !== authGeneration)
      throw new DOMException("Le compte a changé.", "AbortError");
    if (!response.ok)
      throw new APIError(
        response.status,
        typeof data.detail === "string"
          ? data.detail
          : "Vérifiez les paramètres de la demande.",
      );
    return data;
  } finally {
    clearTimeout(timeout);
  }
}
export class SnapshotStore {
  current: Snapshot | null = null;
  received = 0;
  retired = new Set<string>();
  constructor(public owner: string) {}
  accept(frame: Snapshot) {
    if (
      frame.type !== "workspace" ||
      frame.owner !== this.owner ||
      !Number.isFinite(frame.revision) ||
      !Number.isFinite(frame.published_at)
    )
      return false;
    const old = this.current;
    if (old?.source_id === frame.source_id && frame.revision < old.revision)
      return false;
    if (old && old.source_id !== frame.source_id) {
      if (
        this.retired.has(frame.source_id) ||
        frame.published_at < old.published_at
      )
        return false;
      this.retired.add(old.source_id);
    }
    this.current = frame;
    this.received = Date.now();
    return true;
  }
}
export const usd = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value)
    ? "—"
    : new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits:
          Math.abs(value) > 0 && Math.abs(value) < 0.01 ? 4 : 2,
      }).format(Object.is(value, -0) ? 0 : value);
export const short = (address: string) =>
  address ? address.slice(0, 6) + "…" + address.slice(-4) : "—";
export const clock = (value: number) => {
  const n = Math.max(0, Math.floor(value || 0));
  return [Math.floor(n / 3600), Math.floor(n / 60) % 60, n % 60]
    .map((v) => String(v).padStart(2, "0"))
    .join(":");
};
