/** Small shared pieces. Deliberately plain: the backend is the story here. */

import type { ReactNode } from "react";

export function Card({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card">
      <header className="card-header">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p className="subtitle">{subtitle}</p> : null}
        </div>
        {actions ? <div className="card-actions">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}

export function Badge({
  tone = "muted",
  children,
}: {
  tone?: "ok" | "warn" | "bad" | "muted" | "flag";
  children: ReactNode;
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function ErrorBanner({ message }: { message: string }) {
  return (
    <p className="banner banner-error" role="alert">
      {message}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Loading() {
  return <p className="empty">loading…</p>;
}

/** Renders whichever of loading / error / empty / content applies. */
export function PanelBody<T>({
  loading,
  error,
  items,
  emptyMessage,
  children,
}: {
  loading: boolean;
  error: string | null;
  items: T[] | null;
  emptyMessage: string;
  children: (items: T[]) => ReactNode;
}) {
  if (error) return <ErrorBanner message={error} />;
  if (loading && !items) return <Loading />;
  if (!items || items.length === 0) return <Empty>{emptyMessage}</Empty>;
  return <>{children(items)}</>;
}
