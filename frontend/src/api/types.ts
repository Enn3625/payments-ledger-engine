/**
 * Shapes returned by the backend.
 *
 * Every money field is an integer count of MINOR UNITS (paise for INR), the
 * same convention the ledger uses. Nothing here is ever a float, and nothing
 * here should be divided by 100 outside `formatMoney`.
 */

export type Role = "admin" | "viewer";

export type AccountType = "asset" | "liability" | "equity" | "revenue" | "expense";

export type TransactionStatus = "pending" | "posted" | "failed";

export type EntryDirection = "debit" | "credit";

export type AnomalyRule = "velocity" | "amount_threshold";

export type WebhookEventStatus = "received" | "processed" | "ignored" | "failed";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  role: Role;
}

export interface CurrentUser {
  id: string;
  email: string;
  role: Role;
  is_active: boolean;
  created_at: string;
}

export interface AccountBalance {
  account_id: string;
  name: string;
  type: AccountType;
  currency: string;
  debits: number;
  credits: number;
  /** Signed by the account's normal side, so a healthy account reads positive. */
  balance: number;
  entry_count: number;
}

export interface TrialBalance {
  total_debits: number;
  total_credits: number;
  is_balanced: boolean;
}

export interface BalancesResponse {
  accounts: AccountBalance[];
  trial_balance: TrialBalance;
}

export interface LedgerEntry {
  id: string;
  account_id: string;
  account_name: string;
  direction: EntryDirection;
  amount: number;
}

export interface Transaction {
  id: string;
  description: string;
  status: TransactionStatus;
  currency: string;
  amount: number;
  posted_at: string | null;
  created_at: string;
  entries: LedgerEntry[];
  flags: AnomalyRule[];
}

export interface AnomalyFlag {
  id: string;
  rule: AnomalyRule;
  reason: string;
  transaction_id: string | null;
  payment_intent_id: string | null;
  account_id: string | null;
  created_at: string;
}

export interface WebhookEvent {
  id: string;
  event_id: string;
  event_type: string;
  status: WebhookEventStatus;
  attempts: number;
  last_error: string | null;
  transaction_id: string | null;
  payment_intent_id: string | null;
  created_at: string;
  processed_at: string | null;
}

export interface WebhookReceipt {
  event_id: string;
  status: WebhookEventStatus;
  duplicate: boolean;
  transaction_id: string | null;
  payment_intent_id: string | null;
}
