import { Dashboard } from "./components/Dashboard";
import { LoginPage } from "./components/LoginPage";
import { useAuth } from "./auth/AuthContext";

export function App() {
  const { token, user, loading } = useAuth();

  // Wait for /auth/me before deciding: a stored token might be expired,
  // revoked, or belong to an account that has since been deactivated.
  if (token && loading) return <p className="boot">checking session…</p>;
  if (!token || !user) return <LoginPage />;
  return <Dashboard />;
}
