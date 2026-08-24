import { useState } from "react";

import { ApiError, UnauthorizedError, api } from "../api/client";
import type { WebhookEvent } from "../api/types";
import { useApiResource, useAuth } from "../auth/AuthContext";
import { formatDateTime, shortId, statusTone } from "../format";
import { Badge, Card, PanelBody } from "./ui";

export function WebhookEventsPanel({
  refreshKey,
  onRetried,
}: {
  refreshKey: number;
  onRetried: () => void;
}) {
  const { isAdmin } = useAuth();
  const { data, error, loading } = useApiResource<WebhookEvent[]>(
    (token) => api.webhookEvents(token),
    [refreshKey],
  );

  return (
    <Card
      title="Webhook deliveries"
      subtitle={
        isAdmin
          ? "Failed events keep their payload, so they can be replayed as the provider signed them."
          : "Read-only. Retrying a delivery requires an admin."
      }
    >
      <PanelBody
        loading={loading}
        error={error}
        items={data}
        emptyMessage="No webhook deliveries yet."
      >
        {(events) => (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Event</th>
                  <th>Type</th>
                  <th>Status</th>
                  <th className="num">Attempts</th>
                  <th>Result</th>
                  <th>Received</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <EventRow key={event.id} event={event} onRetried={onRetried} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PanelBody>
    </Card>
  );
}

function EventRow({ event, onRetried }: { event: WebhookEvent; onRetried: () => void }) {
  const { token, isAdmin, onUnauthorized } = useAuth();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  async function retry() {
    if (!token) return;
    setBusy(true);
    setResult(null);
    setFailure(null);
    try {
      const receipt = await api.retryWebhookEvent(token, event.event_id);
      // A processed event replays as a no-op rather than posting again, and
      // saying so is more honest than a generic success tick.
      setResult(receipt.duplicate ? "already applied" : "applied");
      onRetried();
    } catch (caught) {
      if (caught instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      setFailure(caught instanceof ApiError ? caught.message : "retry failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <tr className={event.status === "failed" ? "row-flagged" : undefined}>
      <td className="mono">{event.event_id}</td>
      <td className="mono dim">{event.event_type}</td>
      <td>
        <Badge tone={statusTone(event.status)}>{event.status}</Badge>
      </td>
      <td className="num mono">{event.attempts}</td>
      <td>
        {event.last_error ? (
          <span className="error-text">{event.last_error}</span>
        ) : event.transaction_id ? (
          <span className="mono dim" title={event.transaction_id}>
            txn {shortId(event.transaction_id)}
          </span>
        ) : (
          <span className="dim">—</span>
        )}
      </td>
      <td className="dim">{formatDateTime(event.created_at)}</td>
      <td className="num">
        {isAdmin ? (
          <div className="retry-cell">
            <button type="button" onClick={retry} disabled={busy}>
              {busy ? "retrying…" : "Retry"}
            </button>
            {result ? <span className="dim">{result}</span> : null}
            {failure ? <span className="error-text">{failure}</span> : null}
          </div>
        ) : (
          <span className="dim" title="admin only">
            —
          </span>
        )}
      </td>
    </tr>
  );
}
