import { useCallback, useEffect, useState } from "react";
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

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setState((current) => ({ ...current, status: "loading", error: null }));
    try {
      const profile = await api.me(signal);
      setState({
        status: profile ? "authenticated" : "anonymous",
        profile,
        error: null,
      });
      return profile;
    } catch (error) {
      if (signal?.aborted) return null;
      setState({
        status: "error",
        profile: null,
        error: messageFor(error, "We could not check your session."),
      });
      return null;
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  const login = useCallback(async (email: string, password: string) => {
    await api.login({ email, password });
    const profile = await api.me();
    setState({
      status: profile ? "authenticated" : "anonymous",
      profile,
      error: null,
    });
  }, []);

  const register = useCallback(async (email: string, password: string) => {
    await api.register({ email, password });
    await login(email, password);
  }, [login]);

  const logout = useCallback(async () => {
    await api.logout();
    setState({ status: "anonymous", profile: null, error: null });
  }, []);

  return { ...state, refresh, login, register, logout };
}

function messageFor(error: unknown, fallback: string): string {
  if (error instanceof DetouraApiError) {
    if (error.status === 401) return "Invalid email or password.";
    if (error.status === 429) return "Too many attempts. Try again later.";
    if (error.status === 0) return "We couldn't reach Detoura. Check your connection and try again.";
  }
  return fallback;
}
