import { useState } from "react";

import { api } from "../api/client";
import type { Transaction } from "../api/types";
import { useApiResource } from "../auth/AuthContext";
import { RULE_LABELS, formatDateTime, formatMoney, shortId, statusTone } from "../format";
import { Badge, Card, PanelBody } from "./ui";

export function TransactionsPanel({ refreshKey }: { refreshKey: number }) {
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const { data, error, loading } = useApiResource<Transaction[]>(
    (token) => api.transactions(token, flaggedOnly),
    [refreshKey, flaggedOnly],
  );

  return (
    <Card
      title="Transactions"
      subtitle="Every posting, with the entries that make it balance."
      actions={
        <label className="toggle">
          <input
            type="checkbox"
            checked={flaggedOnly}
            onChange={(event) => setFlaggedOnly(event.target.checked)}
          />
          flagged only
        </label>
      }
    >
      <PanelBody
        loading={loading}
        error={error}
        items={data}
        emptyMessage={
          flaggedOnly ? "No flagged transactions." : "No transactions yet. Capture a payment."
        }
      >
        {(transactions) => (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Description</th>
                  <th>Status</th>
                  <th className="num">Amount</th>
                  <th>Entries</th>
                  <th>Flags</th>
                  <th>Posted</th>
                </tr>
              </thead>
              <tbody>
                {transactions.map((transaction) => (
                  <TransactionRow key={transaction.id} transaction={transaction} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelBody>
    </Card>
  );
}

function TransactionRow({ transaction }: { transaction: Transaction }) {
  return (
    <tr className={transaction.flags.length > 0 ? "row-flagged" : undefined}>
      <td className="mono dim" title={transaction.id}>
        {shortId(transaction.id)}
      </td>
      <td>{transaction.description}</td>
      <td>
        <Badge tone={statusTone(transaction.status)}>{transaction.status}</Badge>
      </td>
      <td className="num mono strong">{formatMoney(transaction.amount, transaction.currency)}</td>
      <td>
        {/* Showing both sides makes the double-entry structure visible rather
            than something you have to take on trust. */}
        <ul className="entries">
          {transaction.entries.map((entry) => (
            <li key={entry.id}>
              <span className={`dir dir-${entry.direction}`}>
                {entry.direction === "debit" ? "Dr" : "Cr"}
              </span>
              <span className="mono">{entry.account_name}</span>
              <span className="mono num">{formatMoney(entry.amount, transaction.currency)}</span>
            </li>
          ))}
        </ul>
      </td>
      <td>
        {transaction.flags.length === 0 ? (
          <span className="dim">—</span>
        ) : (
          transaction.flags.map((rule) => (
            <Badge key={rule} tone="flag">
              {RULE_LABELS[rule]}
            </Badge>
          ))
        )}
      </td>
      <td className="dim">{formatDateTime(transaction.posted_at)}</td>
    </tr>
  );
}
