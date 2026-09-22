import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { DetouraApiError, type AccountProfile } from "../api/types";

export type AccountStatus = "loading" | "anonymous" | "authenticated" | "error";

export interface AccountState {
  status: AccountStatus;
  profile: AccountProfile | null;
  error: string | null;
}

const INITIAL: AccountState = {
  status: "loading",
  profile: null,
  error: null,
};

export function useAccount() {
  const [state, setState] = useState<AccountState>(INITIAL);
  const generation = useRef(0);
  const mutating = useRef(false);
  const pendingRevalidation = useRef(false);
  const channel = useRef<BroadcastChannel | null>(null);
  const notifySessionChange = () => {
    try { channel.current?.postMessage("session-changed"); } catch { /* Optional tab coordination. */ }
  };

  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (mutating.current) { pendingRevalidation.current = true; return null; }
    const currentGeneration = ++generation.current;
    setState((current) => ({ ...current, status: "loading", error: null }));
    try {
      const profile = await api.me(signal);
      if (signal?.aborted || currentGeneration !== generation.current) return null;
      setState({
        status: profile ? "authenticated" : "anonymous",
        profile,
        error: null,
      });
      return profile;
    } catch (error) {
      if (signal?.aborted || currentGeneration !== generation.current) return null;
      // Keep the identity boundary stable during a transport failure, without
      // claiming the cached profile is currently authenticated.
      setState((current) => ({
        ...current, status: "error", error: messageFor(error, "We could not check your session."),
      }));
      return null;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const revalidate = () => { void refresh(controller.signal); };
    try {
      channel.current = new BroadcastChannel("detoura-account");
      channel.current.onmessage = (event) => {
        if (event.data === "session-changed") revalidate();
      };
    } catch { /* Focus revalidation remains available without BroadcastChannel. */ }
    // A full-page Google return may have changed the shared cookie too.
    void refresh(controller.signal).then(() => {
      if (!controller.signal.aborted) notifySessionChange();
    });
    window.addEventListener("focus", revalidate);
    window.addEventListener("pageshow", revalidate);
    return () => {
      controller.abort();
      // Deliberately invalidate every outstanding operation on unmount.
      // oxlint-disable-next-line react-hooks/exhaustive-deps
      ++generation.current;
      channel.current?.close();
      channel.current = null;
      window.removeEventListener("focus", revalidate);
      window.removeEventListener("pageshow", revalidate);
    };
  }, [refresh]);

  const finishMutation = useCallback(async () => {
    mutating.current = false;
    if (pendingRevalidation.current) {
      pendingRevalidation.current = false;
      await refresh();
    }
  }, [refresh]);

  const login = useCallback(async (email: string, password: string) => {
    const currentGeneration = ++generation.current;
    mutating.current = true;
    try {
      await api.login({ email, password });
      const profile = await api.me();
      if (currentGeneration !== generation.current) return;
      setState({ status: profile ? "authenticated" : "anonymous", profile, error: null });
      notifySessionChange();
    } catch (error) {
      if (currentGeneration === generation.current) {
        setState({ status: "error", profile: null, error: messageFor(error, "We could not check your session.") });
      }
      throw error;
    } finally {
      await finishMutation();
    }
  }, [finishMutation]);

  const register = useCallback(async (email: string, password: string) => {
    await api.register({ email, password });
    await login(email, password);
  }, [login]);

  const logout = useCallback(async () => {
    const currentGeneration = ++generation.current;
    mutating.current = true;
    try {
      await api.logout();
      if (currentGeneration !== generation.current) return;
      setState({ status: "anonymous", profile: null, error: null });
      notifySessionChange();
    } finally {
      await finishMutation();
    }
  }, [finishMutation]);

  const deleteAccount = useCallback(async (password?: string) => {
    const currentGeneration = ++generation.current;
    mutating.current = true;
    try {
      await api.deleteAccount(password);
      if (currentGeneration !== generation.current) return;
      setState({ status: "anonymous", profile: null, error: null });
      notifySessionChange();
    } finally {
      await finishMutation();
    }
  }, [finishMutation]);

  return { ...state, refresh, login, register, logout, deleteAccount };
}

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof DetouraApiError) {
    if (error.status === 401) return "Invalid email or password.";
    if (error.status === 429) return "Too many attempts. Try again later.";
    if (error.status === 0) return "We couldn't reach Detoura. Check your connection and try again.";
  }
  return fallback;
}
