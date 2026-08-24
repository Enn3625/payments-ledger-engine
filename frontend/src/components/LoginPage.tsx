import { useState } from "react";
import type { FormEvent } from "react";

import { useAuth } from "../auth/AuthContext";
import { ErrorBanner } from "./ui";

export function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("viewer@demo.local");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await login(email.trim(), password);
    } catch (caught) {
      // The API answers identically for a wrong password and an unknown
      // account, so this message stays vague on purpose.
      setError(caught instanceof Error ? caught.message : "could not sign in");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>Payments Ledger</h1>
        <p className="subtitle">Double-entry ledger, idempotent intents, verified webhooks.</p>

        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          autoComplete="username"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
        />

        <label htmlFor="password">Password</label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
        />

        {error ? <ErrorBanner message={error} /> : null}

        <button type="submit" disabled={submitting}>
          {submitting ? "signing in…" : "Sign in"}
        </button>

        <p className="hint">
          A viewer can see everything and change nothing, which is what makes the demo login
          safe to share. Retrying a webhook needs an admin.
        </p>
      </form>
    </main>
  );
}
