import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { DetouraApiError, type AccountProfile } from "../api/types";
import type { GoogleReturnOutcome } from "../lib/googleAuthReturn";
import { AccountPrivacy } from "../components/account/AccountPrivacy";
import "./Login.css";
type AccountMode = "login" | "signup" | "forgot" | "resetRequested" | "resetConfirm" | "passwordUpdated";
type SubmitState = "idle" | "loading" | "invalid" | "error" | "success";
type AuthMethod = "google" | "password" | null;
export type GoogleNotice = Exclude<GoogleReturnOutcome, { kind: "success" }>;
type AccountPayload = {
  email: string;
  password?: string;
};
interface Props {
  initialMode?: AccountMode;
  onLogin?: (payload: Required<AccountPayload>) => void | Promise<void>;
  onSignup?: (payload: Required<AccountPayload>) => void | Promise<void>;
  onDeleteAccount?: (password?: string) => Promise<void>;
  onLogout?: () => void | Promise<void>;
  onForgotPassword?: (payload: Pick<AccountPayload, "email">) => void | Promise<void>;
    onConfirmResetPassword?: (payload: { token: string; newPassword: string }) => void | Promise<void>;
  profile?: AccountProfile | null;
  sessionStatus?: "loading" | "anonymous" | "authenticated" | "error";
  sessionError?: string | null;
  authMethod?: AuthMethod;
    onContinueWithGoogle?: () => void;
    googleRedirecting?: boolean;
    googleNotice?: GoogleNotice | null;
    googleJustSignedIn?: boolean;
    googleLinked?: boolean;
}
const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
export function Login({
  initialMode = "login",
  onLogin,
  onSignup,
  onLogout,
  onDeleteAccount,
  onForgotPassword,
  onConfirmResetPassword,
  profile,
  sessionStatus = "anonymous",
  sessionError,
  authMethod = null,
  onContinueWithGoogle,
  googleRedirecting = false,
  googleNotice,
  googleJustSignedIn = false,
  googleLinked = false,
}: Props) {
  const [mode, setMode] = useState<AccountMode>(initialMode);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [resetCode, setResetCode] = useState("");
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
  const isResetConfirm = mode === "resetConfirm";
  const canSubmit = useMemo(() => {
    if (isLoading) return false;
    if (isForgot) return emailValid;
    if (isResetConfirm) return resetCode.trim().length > 0 && passwordReady && passwordsMatch;
    if (isSignup) return emailValid && passwordReady && passwordsMatch;
    return emailValid && password.length > 0;
  }, [emailValid, isForgot, isResetConfirm, isLoading, isSignup, password.length, passwordReady, passwordsMatch, resetCode]);
  function clearSecrets() {
    setPassword("");
    setConfirmPassword("");
    setResetCode("");
    setShowPassword(false);
    setShowConfirm(false);
  }
  function switchMode(nextMode: AccountMode) {
    setMode(nextMode);
    setSubmitState("idle");
    setSubmitMessage(null);
    clearSecrets();
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
      if (isResetConfirm) {
        await onConfirmResetPassword?.({ token: resetCode.trim(), newPassword: password });
        setSubmitState("success");
        clearSecrets();
        setMode("passwordUpdated");
        return;
      }
      if (isSignup) {
        await onSignup?.({ email, password });
        clearSecrets();
        setSubmitState("success");
        return;
      }
      await onLogin?.({ email, password });
      clearSecrets();
      setSubmitState("success");
    } catch (error) {
      setSubmitMessage(accountErrorMessage(error, mode));
      setSubmitState(isAuthFailure(error) ? "invalid" : "error");
    }
  }
  async function handleLogout() {
    setSubmitState("loading");
    setSubmitMessage(null);
    try {
      await onLogout?.();
      clearSecrets();
      setEmail("");
      setSubmitState("idle");
    } catch (error) {
      setSubmitMessage(accountErrorMessage(error, mode));
      setSubmitState("error");
    }
  }
  return (
    <main className="account-entry" aria-labelledby="account-title">
      <header className="account-entry__header">
        <p className="account-entry__eyebrow">Account</p>
        <h1 id="account-title">Account</h1>
        <p>Manage your sign-in, privacy and account data.</p>
      </header>
      <section className="account-entry__panel" aria-label={panelLabel(mode)}>
        {profile && sessionStatus === "authenticated" ? (
          <div className="account-confirm" aria-live="polite">
            <span className="account-confirm__avatar" aria-hidden="true">{profile.email_normalized.slice(0, 1).toUpperCase()}</span>
            <div className="account-confirm__identity">
              <p className="account-confirm__email">{profile.email_normalized}</p>
              <p className="account-confirm__method">
                {authMethod === "google" || googleJustSignedIn ? "Signed in with Google" : authMethod === "password" ? "Signed in with password" : "Signed in to Detoura"}
                {googleLinked && !googleJustSignedIn ? " · Google connected" : ""}
              </p>
            </div>
            {(googleJustSignedIn || googleLinked) && <span className="account-confirm__google">Google</span>}
            {googleNotice?.kind === "error" && (
              <p role="status" className="account-form__status account-form__status--error account-confirm__status">{googleNoticeMessage(googleNotice)}</p>
            )}
            {submitState === "error" && <p role="alert" className="account-form__status account-form__status--error account-confirm__status">{submitMessage}</p>}
            <button type="button" className="account-form__submit account-confirm__logout" onClick={handleLogout} disabled={isLoading}>
              {isLoading ? "Signing out…" : "Sign out"}
            </button>
            {onDeleteAccount && <AccountPrivacy onDelete={onDeleteAccount} />}
          </div>
        ) : mode === "resetRequested" || mode === "passwordUpdated" ? (
          <ConfirmationView
            mode={mode}
            onBack={() => switchMode("login")}
            onEnterCode={mode === "resetRequested" ? () => switchMode("resetConfirm") : undefined}
          />
        ) : (
          <>
            <div className="account-entry__head">
              <p>{isSignup ? "Create account" : isForgot || isResetConfirm ? "Password help" : "Welcome back"}</p>
              <h2>
                {isSignup
                  ? "Create your Detoura account"
                  : isForgot
                    ? "Reset your password"
                    : isResetConfirm
                      ? "Enter your reset code"
                      : "Log in to Detoura"}
              </h2>
            </div>
            {isLogin && googleNotice && (
              <p
                id="google-notice"
                role="status"
                aria-live="polite"
                className={
                  googleNotice.kind === "error"
                    ? "account-google-notice account-google-notice--error"
                    : "account-google-notice"
                }
              >
                {googleNoticeMessage(googleNotice)}
              </p>
            )}
            {(isLogin || isSignup) && onContinueWithGoogle && (
              <>
                <button
                  type="button"
                  className="account-google"
                  onClick={onContinueWithGoogle}
                  disabled={googleRedirecting}
                  aria-describedby={isLogin && googleNotice ? "google-notice" : undefined}
                >
                  <GoogleMark />
                  {googleRedirecting ? "Connecting to Google…" : "Continue with Google"}
                </button>
                <div className="account-divider" role="separator" aria-label="or">
                  <span>or</span>
                </div>
              </>
            )}
            <form className="account-form" onSubmit={handleSubmit} noValidate>
              {!isResetConfirm && (
                <Field
                  id="account-email"
                  label="Email"
                  type="email"
                  value={email}
                  onChange={setEmail}
                  autoComplete="email"
                  error={email && !emailValid ? "Enter a valid email address." : undefined}
                />
              )}
              {isResetConfirm && (
                <Field
                  id="account-reset-code"
                  label="Reset code"
                  type="text"
                  value={resetCode}
                  onChange={setResetCode}
                  autoComplete="one-time-code"
                />
              )}
              {!isForgot && (
                <PasswordField
                  id="account-password"
                  label={isResetConfirm ? "New password" : "Password"}
                  value={password}
                  onChange={setPassword}
                  visible={showPassword}
                  onToggle={() => setShowPassword(value => !value)}
                  autoComplete={isSignup || isResetConfirm ? "new-password" : "current-password"}
                  help={isSignup || isResetConfirm ? "Use at least 8 characters." : undefined}
                  error={(isSignup || isResetConfirm) && password && !passwordReady ? "Password must be at least 8 characters." : undefined}
                />
              )}
              {(isSignup || isResetConfirm) && (
                <PasswordField
                  id="account-confirm-password"
                  label={isResetConfirm ? "Confirm new password" : "Confirm password"}
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
                {isLoading
                  ? "Please wait..."
                  : isSignup
                    ? "Create account"
                    : isForgot
                      ? "Send reset email"
                      : isResetConfirm
                        ? "Update password"
                        : "Log in"}
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
              {isResetConfirm && (
                <>
                  <button type="button" onClick={() => switchMode("forgot")}>Request a new code</button>
                  <button type="button" onClick={() => switchMode("login")}>Back to login</button>
                </>
              )}
            </div>
          </>
        )}
        <a className="account-entry__privacy" href="/privacy">Privacy Notice</a>
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
      ? mode === "resetConfirm"
        ? "This reset code is invalid or has expired."
        : "We could not log you in with those details."
      : state === "error"
        ? "Something went wrong. Please try again."
        : mode === "signup"
          ? "Account created. You are signed in."
          : "Signed in.";
  return <p id={id} className={`account-form__status account-form__status--${state}`} aria-live="polite">{message ?? fallback}</p>;
}
function ConfirmationView({
  mode,
  onBack,
  onEnterCode,
}: {
  mode: AccountMode;
  onBack: () => void;
  onEnterCode?: () => void;
}) {
  const resetRequested = mode === "resetRequested";
  return (
    <div className="account-confirm account-confirm--message" aria-live="polite">
      <span aria-hidden="true">✓</span>
      <p>{resetRequested ? "Check your email" : "Password updated"}</p>
      <h2>
        {resetRequested
          ? "If an account exists for that email, we've sent a reset code."
          : "Your password has been updated."}
      </h2>
      {resetRequested && onEnterCode ? (
        <>
          <button type="button" className="account-form__submit" onClick={onEnterCode}>
            I have a reset code
          </button>
          <div className="account-entry__switch">
            <button type="button" onClick={onBack}>Back to login</button>
          </div>
        </>
      ) : (
        <button type="button" className="account-form__submit" onClick={onBack}>Sign in</button>
      )}
    </div>
  );
}
function panelLabel(mode: AccountMode) {
  if (mode === "signup") return "Create account";
  if (mode === "forgot" || mode === "resetRequested" || mode === "resetConfirm") return "Password reset";
  if (mode === "passwordUpdated") return "Password updated";
  return "Log in";
}
function isAuthFailure(error: unknown): boolean {
  return error instanceof DetouraApiError && (error.status === 401 || error.status === 400);
}
function accountErrorMessage(error: unknown, mode: AccountMode): string {
  if (error instanceof DetouraApiError) {
    if (mode === "resetConfirm") {
      if (error.status === 429) return "Too many attempts. Try again later.";
      if (error.status === 0) return "We couldn't reach Detoura. Check your connection and try again.";
      // Deliberately generic for any other failure (400 = invalid/expired/
      // used token) - the backend itself never distinguishes why a reset
      // token failed (§10 "single use"), so the frontend does not invent a
      // distinction either.
      return "This reset code is invalid or has expired. Request a new one.";
    }
    if (error.status === 401) return "Invalid email or password.";
    if (error.status === 429) return "Too many attempts. Try again later.";
    if (error.status === 400) return error.message || "We could not create that account.";
    if (error.status === 0) return "We couldn't reach Detoura. Check your connection and try again.";
  }
  return "Something went wrong. Please try again.";
}

function googleNoticeMessage(notice: GoogleNotice): string {
  if (notice.kind === "link_required") {
    return "An account with this email already exists. Log in with your password below to verify it's you, and Detoura will connect your Google account.";
  }
  switch (notice.reason) {
    case "cancelled":
      return "Google sign-in was cancelled.";
    case "link_conflict":
      return "This Google account is already connected to a different Detoura account.";
    case "failed":
    default:
      return "We couldn't complete Google sign-in. Please try again.";
  }
}

function GoogleMark() {
  return (
    <svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true" focusable="false">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62Z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.34 0-4.33-1.58-5.04-3.71H.96v2.33A9 9 0 0 0 9 18Z" />
      <path fill="#FBBC05" d="M3.96 10.71A5.4 5.4 0 0 1 3.68 9c0-.59.1-1.17.28-1.71V4.96H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.04l3-2.33Z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.51.46 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.96l3 2.33C4.67 5.16 6.66 3.58 9 3.58Z" />
    </svg>
  );
}
