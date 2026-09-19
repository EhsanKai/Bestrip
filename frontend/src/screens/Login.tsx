import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { DetouraApiError, type AccountProfile } from "../api/types";
import "./Login.css";

type AccountMode = "login" | "signup" | "forgot" | "resetRequested" | "passwordUpdated";
type SubmitState = "idle" | "loading" | "invalid" | "error" | "success";

type AccountPayload = {
  email: string;
  password?: string;
};

interface Props {
  initialMode?: AccountMode;
  onLogin?: (payload: Required<AccountPayload>) => void | Promise<void>;
  onSignup?: (payload: Required<AccountPayload>) => void | Promise<void>;
  onLogout?: () => void | Promise<void>;
  onForgotPassword?: (payload: Pick<AccountPayload, "email">) => void | Promise<void>;
  profile?: AccountProfile | null;
  sessionStatus?: "loading" | "anonymous" | "authenticated" | "error";
  sessionError?: string | null;
}

const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function Login({
  initialMode = "login",
  onLogin,
  onSignup,
  onLogout,
  onForgotPassword,
  profile,
  sessionStatus = "anonymous",
  sessionError,
}: Props) {
  const [mode, setMode] = useState<AccountMode>(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);
  const [submitState, setSubmitState] = useState<SubmitState>("idle");
  const [submitMessage, setSubmitMessage] = useState<string | null>(null);

  const emailValid = emailPattern.test(email);
  const passwordReady = password.length >= 8;
  const passwordsMatch = password === confirmPassword;
  const isLoading = submitState === "loading";
  const statusId = "account-status";
  const isLogin = mode === "login";
  const isSignup = mode === "signup";
  const isForgot = mode === "forgot";

  const canSubmit = useMemo(() => {
    if (isLoading) return false;
    if (isForgot) return emailValid;
    if (isSignup) return emailValid && passwordReady && passwordsMatch;
    return emailValid && password.length > 0;
  }, [emailValid, isForgot, isLoading, isSignup, password.length, passwordReady, passwordsMatch]);

  function switchMode(nextMode: AccountMode) {
    setMode(nextMode);
    setSubmitState("idle");
    setSubmitMessage(null);
    setPassword("");
    setConfirmPassword("");
    setShowPassword(false);
    setShowConfirm(false);
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmitState("loading");
    setSubmitMessage(null);
    try {
      if (isForgot) {
        await onForgotPassword?.({ email });
        setSubmitState("success");
        setMode("resetRequested");
        return;
      }
      if (isSignup) {
        await onSignup?.({ email, password });
        setSubmitState("success");
        return;
      }
      await onLogin?.({ email, password });
      setSubmitState("success");
    } catch (error) {
      setSubmitMessage(accountErrorMessage(error));
      setSubmitState(isAuthFailure(error) ? "invalid" : "error");
    }
  }

  async function handleLogout() {
    setSubmitState("loading");
    setSubmitMessage(null);
    try {
      await onLogout?.();
      setPassword("");
      setConfirmPassword("");
      setSubmitState("idle");
    } catch (error) {
      setSubmitMessage(accountErrorMessage(error));
      setSubmitState("error");
    }
  }

  return (
    <main className="account-entry" aria-labelledby="account-title">
      <section className="account-entry__visual" aria-label="Detoura account">
        <div>
          <p className="account-entry__eyebrow">Detoura account</p>
          <h1 id="account-title">Your next thoughtful trip starts here.</h1>
          <p>
            Save ideas, compare routes, and return to the journeys that still
            feel worth taking.
          </p>
        </div>
        <span className="account-entry__location">Lake Como, Italy</span>
      </section>

      <section className="account-entry__panel" aria-label={panelLabel(mode)}>
        {profile && sessionStatus === "authenticated" ? (
          <div className="account-confirm" aria-live="polite">
            <span aria-hidden="true">✓</span>
            <p>Signed in</p>
            <h2>{profile.email_normalized}</h2>
            <button type="button" className="account-form__submit" onClick={handleLogout} disabled={isLoading}>
              {isLoading ? "Signing out..." : "Log out"}
            </button>
          </div>
        ) : mode === "resetRequested" || mode === "passwordUpdated" ? (
          <ConfirmationView mode={mode} onBack={() => switchMode("login")} />
        ) : (
          <>
            <div className="account-entry__head">
              <p>{isSignup ? "Create account" : isForgot ? "Password help" : "Welcome back"}</p>
              <h2>{isSignup ? "Create your Detoura account" : isForgot ? "Reset your password" : "Log in to Detoura"}</h2>
            </div>

            <form className="account-form" onSubmit={handleSubmit} noValidate>
              <Field
                id="account-email"
                label="Email"
                type="email"
                value={email}
                onChange={setEmail}
                autoComplete="email"
                error={email && !emailValid ? "Enter a valid email address." : undefined}
              />

              {!isForgot && (
                <PasswordField
                  id="account-password"
                  label="Password"
                  value={password}
                  onChange={setPassword}
                  visible={showPassword}
                  onToggle={() => setShowPassword(value => !value)}
                  autoComplete={isSignup ? "new-password" : "current-password"}
                  help={isSignup ? "Use at least 8 characters." : undefined}
                  error={isSignup && password && !passwordReady ? "Password must be at least 8 characters." : undefined}
                />
              )}

              {isSignup && (
                <PasswordField
                  id="account-confirm-password"
                  label="Confirm password"
                  value={confirmPassword}
                  onChange={setConfirmPassword}
                  visible={showConfirm}
                  onToggle={() => setShowConfirm(value => !value)}
                  autoComplete="new-password"
                  error={confirmPassword && !passwordsMatch ? "Passwords do not match." : undefined}
                />
              )}

              {sessionStatus === "loading" && (
                <p className="account-form__status" role="status">Checking session...</p>
              )}
              {sessionError && (
                <p className="account-form__status account-form__status--error">{sessionError}</p>
              )}

              <StatusMessage state={submitState} mode={mode} id={statusId} message={submitMessage} />

              <button className="account-form__submit" type="submit" disabled={!canSubmit} aria-describedby={statusId}>
                {isLoading ? "Please wait..." : isSignup ? "Create account" : isForgot ? "Send reset email" : "Log in"}
              </button>
            </form>

            <div className="account-entry__switch">
              {isLogin && (
                <>
                  <button type="button" onClick={() => switchMode("forgot")}>Forgot password?</button>
                  <button type="button" onClick={() => switchMode("signup")}>Create account</button>
                </>
              )}
              {isSignup && <button type="button" onClick={() => switchMode("login")}>Already have an account? Log in</button>}
              {isForgot && <button type="button" onClick={() => switchMode("login")}>Back to login</button>}
            </div>
          </>
        )}
      </section>
    </main>
  );
}

function Field({
  id,
  label,
  type,
  value,
  onChange,
  autoComplete,
  error,
}: {
  id: string;
  label: string;
  type: string;
  value: string;
  onChange: (value: string) => void;
  autoComplete: string;
  error?: string;
}) {
  const errorId = `${id}-error`;
  return (
    <div className="account-field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        type={type}
        value={value}
        onChange={event => onChange(event.target.value)}
        autoComplete={autoComplete}
        aria-invalid={Boolean(error) || undefined}
        aria-describedby={error ? errorId : undefined}
      />
      {error && <p id={errorId} className="account-field__error">{error}</p>}
    </div>
  );
}

function PasswordField({
  id,
  label,
  value,
  onChange,
  visible,
  onToggle,
  autoComplete,
  help,
  error,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  visible: boolean;
  onToggle: () => void;
  autoComplete: string;
  help?: string;
  error?: string;
}) {
  const helpId = help ? `${id}-help` : undefined;
  const errorId = `${id}-error`;
  const describedBy = [helpId, error ? errorId : undefined].filter(Boolean).join(" ") || undefined;
  return (
    <div className="account-field">
      <label htmlFor={id}>{label}</label>
      <div className="account-field__password">
        <input
          id={id}
          type={visible ? "text" : "password"}
          value={value}
          onChange={event => onChange(event.target.value)}
          autoComplete={autoComplete}
          aria-invalid={Boolean(error) || undefined}
          aria-describedby={describedBy}
        />
        <button type="button" onClick={onToggle} aria-label={visible ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`}>
          {visible ? "Hide" : "Show"}
        </button>
      </div>
      {help && <p id={helpId} className="account-field__help">{help}</p>}
      {error && <p id={errorId} className="account-field__error">{error}</p>}
    </div>
  );
}

function StatusMessage({
  state,
  mode,
  id,
  message,
}: {
  state: SubmitState;
  mode: AccountMode;
  id: string;
  message?: string | null;
}) {
  if (state === "idle") return <p id={id} className="account-form__status" aria-live="polite" />;
  const fallback = state === "loading"
    ? "Submitting securely..."
    : state === "invalid"
      ? "We could not log you in with those details."
      : state === "error"
        ? "Something went wrong. Please try again."
        : mode === "signup"
          ? "Account created. You are signed in."
          : "Signed in.";
  return <p id={id} className={`account-form__status account-form__status--${state}`} aria-live="polite">{message ?? fallback}</p>;
}

function ConfirmationView({ mode, onBack }: { mode: AccountMode; onBack: () => void }) {
  const resetRequested = mode === "resetRequested";
  return (
    <div className="account-confirm" aria-live="polite">
      <span aria-hidden="true">✓</span>
      <p>{resetRequested ? "Check your email" : "Password updated"}</p>
      <h2>{resetRequested ? "If an account exists, reset instructions are on their way." : "You can return to Detoura and log in again."}</h2>
      <button type="button" className="account-form__submit" onClick={onBack}>Back to login</button>
    </div>
  );
}

function panelLabel(mode: AccountMode) {
  if (mode === "signup") return "Create account";
  if (mode === "forgot" || mode === "resetRequested") return "Password reset";
  if (mode === "passwordUpdated") return "Password updated";
  return "Log in";
}

function isAuthFailure(error: unknown): boolean {
  return error instanceof DetouraApiError && (error.status === 401 || error.status === 400);
}

function accountErrorMessage(error: unknown): string {
  if (error instanceof DetouraApiError) {
    if (error.status === 401) return "Invalid email or password.";
    if (error.status === 429) return "Too many attempts. Try again later.";
    if (error.status === 400) return error.message || "We could not create that account.";
    if (error.status === 0) return "We couldn't reach Detoura. Check your connection and try again.";
  }
  return "Something went wrong. Please try again.";
}
