/**
 * Who is signed in, resolved once and shared.
 *
 * The three states are distinct and the interface treats them differently: `loading`
 * means Firebase has not yet answered, `null` means signed out, and a user means
 * signed in. Collapsing loading into signed-out is what makes an app flash its
 * sign-in screen on every refresh before recognising the session.
 */
"use client";

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import type { User } from "firebase/auth";

import { signOut as firebaseSignOut, watchUser } from "./auth";

interface Session {
  user: User | null;
  loading: boolean;
  signOut: () => Promise<void>;
}

const SessionContext = createContext<Session>({
  user: null,
  loading: true,
  signOut: async () => {},
});

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    return watchUser((next) => {
      setUser(next);
      setLoading(false);
    });
  }, []);

  const value = useMemo<Session>(
    () => ({ user, loading, signOut: firebaseSignOut }),
    [user, loading],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  return useContext(SessionContext);
}

/**
 * Send signed-out visitors to the sign-in page.
 *
 * Returns `ready`, which is false while Firebase is still answering. A page that
 * renders before that resolves would fetch with no token and show an error state for
 * a session that is about to be valid.
 */
export function useRequireSession(): { user: User | null; ready: boolean } {
  const { user, loading } = useSession();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/sign-in");
  }, [loading, user, router]);

  return { user, ready: !loading && Boolean(user) };
}
