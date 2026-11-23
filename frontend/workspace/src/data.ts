import type { Config, Snapshot } from "./types";
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
    const text = await response.text();
    let data: any = {};
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        // A proxy or ASGI 500 can answer in plain text or HTML. Never surface
        // a JSON parser error to the user.
        data = {
          detail:
            response.status >= 500
              ? "Erreur serveur (" + response.status + "). Réessayez."
              : text.slice(0, 200),
        };
      }
    }
    if (path !== "/config" && generation !== authGeneration)
      throw new DOMException("Le compte a changé.", "AbortError");
    if (!response.ok)
      throw new APIError(
        response.status,
        typeof data?.detail === "string"
          ? data.detail
          : "Vérifiez les paramètres de la demande.",
      );
    return data;
  } finally {
    clearTimeout(timeout);
  }
}
export function workspaceSocketUrl(
  config: Pick<Config, "ws_origin"> | null | undefined,
  pageOrigin: string,
  ticket: string,
): string | null {
  const raw = config?.ws_origin?.trim();
  if (!raw) return null;
  let origin: URL;
  try {
    origin = new URL(raw);
  } catch {
    return null;
  }
  const page = new URL(pageOrigin);
  // A same-origin Vercel socket cannot be upgraded; keep the REST fallback.
  if (origin.host === page.host && !["localhost", "127.0.0.1", "::1"].includes(page.hostname))
    return null;
  const protocol =
    origin.protocol === "https:"
      ? "wss:"
      : origin.protocol === "http:"
        ? "ws:"
        : origin.protocol;
  if (protocol !== "wss:" && protocol !== "ws:") return null;
  const url = new URL("/api/v2/ws", origin);
  url.protocol = protocol;
  url.searchParams.set("ticket", ticket);
  return url.toString();
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
