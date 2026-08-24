import { api } from "../api/client";
import type { AnomalyFlag } from "../api/types";
import { useApiResource } from "../auth/AuthContext";
import { RULE_LABELS, formatDateTime, shortId } from "../format";
import { Badge, Card, PanelBody } from "./ui";

export function FlagsPanel({ refreshKey }: { refreshKey: number }) {
  const { data, error, loading } = useApiResource<AnomalyFlag[]>(
    (token) => api.anomalyFlags(token),
    [refreshKey],
  );

  return (
    <Card
      title="Anomaly flags"
      subtitle="Advisory only. A flag never blocks a capture or changes the ledger."
    >
      <PanelBody
        loading={loading}
        error={error}
        items={data}
        emptyMessage="Nothing flagged."
      >
        {(flags) => (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Reason</th>
                  <th>Transaction</th>
                  <th>Raised</th>
                </tr>
              </thead>
              <tbody>
                {flags.map((flag) => (
                  <tr key={flag.id}>
                    <td>
                      <Badge tone="flag">{RULE_LABELS[flag.rule]}</Badge>
                    </td>
                    {/* The reason carries the numbers that tripped the rule, so
                        a reviewer can reconstruct the decision. */}
                    <td>{flag.reason}</td>
                    <td className="mono dim" title={flag.transaction_id ?? ""}>
                      {shortId(flag.transaction_id)}
                    </td>
                    <td className="dim">{formatDateTime(flag.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelBody>
    </Card>
  );
}
