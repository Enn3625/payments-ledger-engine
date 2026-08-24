import { useCallback, useState } from "react";

import { useAuth } from "../auth/AuthContext";
import { BalancesPanel } from "./BalancesPanel";
import { FlagsPanel } from "./FlagsPanel";
import { TransactionsPanel } from "./TransactionsPanel";
import { WebhookEventsPanel } from "./WebhookEventsPanel";
import { Badge } from "./ui";

export function Dashboard() {
  const { user, logout } = useAuth();
  // One counter drives every panel: a retry changes balances, transactions and
  // flags at once, so they must not refresh independently and disagree.
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((key) => key + 1), []);

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>Payments Ledger</h1>
          <p className="subtitle">Double-entry ledger, idempotent intents, verified webhooks.</p>
        </div>
        <div className="app-header-right">
          <span className="mono dim">{user?.email}</span>
          <Badge tone={user?.role === "admin" ? "ok" : "muted"}>{user?.role}</Badge>
          <button type="button" onClick={refresh}>
            Refresh
          </button>
          <button type="button" className="secondary" onClick={logout}>
            Sign out
          </button>
        </div>
      </header>

      <main className="panels">
        <BalancesPanel refreshKey={refreshKey} />
        <TransactionsPanel refreshKey={refreshKey} />
        <FlagsPanel refreshKey={refreshKey} />
        <WebhookEventsPanel refreshKey={refreshKey} onRetried={refresh} />
      </main>

      <footer className="app-footer">
        <span>
          Amounts are integer minor units (paise). The ledger never stores money as a float.
        </span>
      </footer>
    </div>
  );
}
