/**
 * The single place that talks to the API.
 *
 * Two rules it enforces for everyone else:
 *   - the bearer token is attached here, never sprinkled through components;
 *   - a 401 means the session is over, so it is surfaced as a distinct error
 *     type the app can react to by logging out rather than showing a red box.
 */

import type {
  AnomalyFlag,
  BalancesResponse,
  CurrentUser,
  TokenResponse,
  Transaction,
  WebhookEvent,
  WebhookEventStatus,
  WebhookReceipt,
} from "./types";

const BASE_URL = (import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Raised on 401 so the app can end the session instead of rendering an error. */
export class UnauthorizedError extends ApiError {
  constructor(message = "session expired") {
    super(401, message);
    this.name = "UnauthorizedError";
  }
}

/**
 * `fetch` rejects with the same opaque TypeError whether the API is down or
 * the browser blocked a cross-origin response. Guessing wrong sends you
 * debugging the wrong layer, so name both possibilities.
 */
function unreachable(): ApiError {
  return new ApiError(
    0,
    `could not read a response from ${BASE_URL} — the API may be down, or it may ` +
      `have answered without allowing this origin (${window.location.origin}). ` +
      `Check the browser console for a CORS error, and that CORS_ORIGINS on the ` +
      `backend includes ${window.location.origin}.`,
  );
}

async function readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) return body.detail[0].msg;
    return response.statusText || `request failed (${response.status})`;
  } catch {
    return response.statusText || `request failed (${response.status})`;
  }
}

async function request<T>(path: string, token: string | null, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    });
  } catch {
    throw unreachable();
  }

  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) throw new ApiError(response.status, await readError(response));
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  /** OAuth2 password flow: the field is called `username`, the value is an email. */
  async login(email: string, password: string): Promise<TokenResponse> {
    const form = new URLSearchParams({ username: email, password });
    const response = await fetch(`${BASE_URL}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    }).catch(() => {
      throw unreachable();
    });

    if (!response.ok) throw new ApiError(response.status, await readError(response));
    return (await response.json()) as TokenResponse;
  },

  me: (token: string) => request<CurrentUser>("/auth/me", token),

  balances: (token: string) => request<BalancesResponse>("/accounts/balances", token),

  transactions: (token: string, flaggedOnly = false) =>
    request<Transaction[]>(`/transactions?flagged_only=${flaggedOnly}&limit=100`, token),

  anomalyFlags: (token: string) => request<AnomalyFlag[]>("/anomaly-flags?limit=100", token),

  webhookEvents: (token: string, status?: WebhookEventStatus) =>
    request<WebhookEvent[]>(
      `/webhooks/events?limit=100${status ? `&status=${status}` : ""}`,
      token,
    ),

  /** Admin only. Replays the stored payload; a processed event is a no-op. */
  retryWebhookEvent: (token: string, eventId: string) =>
    request<WebhookReceipt>(`/webhooks/events/${encodeURIComponent(eventId)}/retry`, token, {
      method: "POST",
    }),
};

export { BASE_URL };
