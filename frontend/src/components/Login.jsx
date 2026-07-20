import React, { useState } from "react";
import { api } from "../api.js";
import { describeError } from "../lib/errors.js";

export default function Login({ onLogin }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const r = await api.login(username, password);
      onLogin(r.username);
    } catch (err) {
      setError(describeError(err, "Login failed."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-shell">
      <form className="login-card" onSubmit={submit}>
        <div className="brand-mark big">FMT</div>
        <h1>Find My Trial</h1>
        <p className="muted">Sign in to the clinical review workspace.</p>
        {error && (
          <div className="banner error" role="alert">
            <div className="banner-text">
              {error.message}
              {error.errorId && <> Reference <code>{error.errorId}</code>.</>}
            </div>
          </div>
        )}
        <label htmlFor="login-username">
          <span>Username</span>
          <input id="login-username" name="username" autoComplete="username"
            value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </label>
        <label htmlFor="login-password">
          <span>Password</span>
          <input id="login-password" name="password" type="password" autoComplete="current-password"
            value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <button className="primary" disabled={busy} type="submit">
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <span className="sr-only" role="status" aria-live="polite">{busy ? "Signing in…" : ""}</span>
        <p className="fineprint">
          Decision support only. Handles de-identified clinical text. Sessions expire after inactivity.
        </p>
      </form>
    </div>
  );
}
