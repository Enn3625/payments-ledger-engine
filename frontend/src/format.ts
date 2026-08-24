/**
 * Display helpers.
 *
 * `formatMoney` is the ONLY place minor units become a decimal string, and it
 * does the split with integer arithmetic. Dividing by 100 anywhere else would
 * reintroduce exactly the floating-point error the ledger avoids by storing
 * paise as integers.
 */

import type { AnomalyRule, TransactionStatus, WebhookEventStatus } from "./api/types";

export function formatMoney(minorUnits: number, currency = "INR"): string {
  const negative = minorUnits < 0;
  const absolute = Math.abs(minorUnits);
  const major = Math.trunc(absolute / 100);
  const minor = absolute % 100;
  const grouped = major.toLocaleString("en-IN");
  return `${negative ? "-" : ""}${currency} ${grouped}.${String(minor).padStart(2, "0")}`;
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function shortId(id: string | null): string {
  if (!id) return "—";
  return id.length <= 12 ? id : `${id.slice(0, 8)}…`;
}

export const RULE_LABELS: Record<AnomalyRule, string> = {
  velocity: "velocity",
  amount_threshold: "amount",
};

export function statusTone(
  status: TransactionStatus | WebhookEventStatus,
): "ok" | "warn" | "bad" | "muted" {
  switch (status) {
    case "posted":
    case "processed":
      return "ok";
    case "failed":
      return "bad";
    case "pending":
    case "received":
      return "warn";
    default:
      return "muted";
  }
}
