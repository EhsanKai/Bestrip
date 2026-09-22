import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "../../api/client";
import { DetouraApiError } from "../../api/types";
import { legalConfig, legalReadinessIssues } from "../../privacy/legalConfig";
import "./AccountPrivacy.css";

export function AccountPrivacy({ onDelete }: { onDelete: (password?: string) => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const active = useRef(true);
  const pending = useRef(false);
  const download = useRef<AbortController | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const email = legalReadinessIssues().some(issue => issue.startsWith("PRIVACY_CONTACT_EMAIL")) ? "" : legalConfig.PRIVACY_CONTACT_EMAIL;
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; download.current?.abort(); };
  }, []);
  function closeDialog() {
    dialog.current?.close();
    setPassword("");
    setDeleteError("");
    trigger.current?.focus();
  }
  async function exportData() {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setMessage("");
    const controller = new AbortController();
    download.current = controller;
    try {
      const data = await api.exportAccountData(controller.signal);
      if (!active.current || controller.signal.aborted) return;
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "detoura-account-data.json";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setMessage("Account data download started.");
    } catch {
      if (active.current && !controller.signal.aborted) setMessage("Account data could not be exported. Check your sign-in and try again.");
    } finally {
      pending.current = false;
      if (active.current) setBusy(false);
    }
  }
  async function deleteAccount(event: FormEvent) {
    event.preventDefault();
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setDeleteError("");
    try {
      await onDelete(password || undefined);
      if (active.current) closeDialog();
    } catch (error) {
      if (active.current) {
        setPassword("");
        setDeleteError(error instanceof DetouraApiError && error.status === 400
          ? "Check your current password if your account has one, then try again."
          : "We could not confirm account deletion. Check your sign-in before trying again.");
      }
    } finally {
      pending.current = false;
      if (active.current) setBusy(false);
    }
  }
  return (
    <section className="account-privacy" aria-labelledby="account-privacy-title">
      <h3 id="account-privacy-title">Account privacy</h3>
      <p>Exports the account information currently available through Detoura. For a broader privacy request, contact the privacy contact shown in our <a href="/privacy">Privacy Notice</a>.</p>
      {email && <a href={`mailto:${email}`}>{email}</a>}
      <button type="button" onClick={exportData} disabled={busy}>Export account data</button>
      <p role="status">{message}</p>
      <button type="button" ref={trigger} className="account-privacy__destructive" disabled={busy} onClick={() => dialog.current?.showModal()}>Delete account</button>
      <dialog ref={dialog} className="account-privacy__dialog" aria-labelledby="delete-account-title" aria-describedby="delete-account-description" onCancel={event => { event.preventDefault(); if (!pending.current) closeDialog(); }}>
        <form onSubmit={deleteAccount}>
          <h2 id="delete-account-title">Delete your account?</h2>
          <p id="delete-account-description">Your Detoura account and sign-in access will be removed. Some booking, payment and financial records may need to be kept for legal or record-keeping purposes.</p>
          <a href="/privacy">Privacy Notice</a>
          <label htmlFor="delete-account-password">Current password (if your account has one)</label>
          <input id="delete-account-password" type="password" autoComplete="current-password" maxLength={1000} value={password} onChange={event => setPassword(event.target.value)} disabled={busy} />
          <p>For an account with only Google Sign-In, leave this blank.</p>
          {deleteError && <p role="alert">{deleteError}</p>}
          <div className="account-privacy__actions">
            <button type="button" autoFocus onClick={closeDialog} disabled={busy}>Cancel</button>
            <button type="submit" className="account-privacy__destructive" disabled={busy}>{busy ? "Deleting…" : "Delete account"}</button>
          </div>
        </form>
      </dialog>
    </section>
  );
}
