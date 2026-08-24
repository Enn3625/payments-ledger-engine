import { api } from "../api/client";
import type { BalancesResponse } from "../api/types";
import { useApiResource } from "../auth/AuthContext";
import { formatMoney } from "../format";
import { Badge, Card, ErrorBanner, Loading } from "./ui";

export function BalancesPanel({ refreshKey }: { refreshKey: number }) {
  const { data, error, loading } = useApiResource<BalancesResponse>(
    (token) => api.balances(token),
    [refreshKey],
  );

  return (
    <Card
      title="Ledger balances"
      subtitle="Signed by each account's normal side, so a healthy account reads positive."
      actions={data ? <TrialBalanceBadge data={data} /> : null}
    >
      {error ? <ErrorBanner message={error} /> : null}
      {!data && loading ? <Loading /> : null}
      {data ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Account</th>
                <th>Type</th>
                <th className="num">Debits</th>
                <th className="num">Credits</th>
                <th className="num">Balance</th>
                <th className="num">Entries</th>
              </tr>
            </thead>
            <tbody>
              {data.accounts.map((account) => (
                <tr key={account.account_id}>
                  <td className="mono">{account.name}</td>
                  <td>
                    <Badge>{account.type}</Badge>
                  </td>
                  <td className="num mono">{formatMoney(account.debits, account.currency)}</td>
                  <td className="num mono">{formatMoney(account.credits, account.currency)}</td>
                  <td className="num mono strong">
                    {formatMoney(account.balance, account.currency)}
                  </td>
                  <td className="num mono dim">{account.entry_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Card>
  );
}

/**
 * The headline invariant. Postgres refuses to commit an unbalanced
 * transaction, so this should read "balanced" forever -- and if it ever does
 * not, that is the first thing anyone needs to see.
 */
function TrialBalanceBadge({ data }: { data: BalancesResponse }) {
  const { total_debits, total_credits, is_balanced } = data.trial_balance;
  return (
    <div className="trial-balance">
      <Badge tone={is_balanced ? "ok" : "bad"}>
        {is_balanced ? "debits = credits" : "UNBALANCED"}
      </Badge>
      <span className="mono dim">
        {formatMoney(total_debits)} / {formatMoney(total_credits)}
      </span>
    </div>
  );
}
