"use client";

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";

import { describeAuthError, register, signIn } from "@/lib/auth";
import { isEmulated, config } from "@/lib/config";
import { useSession } from "@/lib/session";
import { ErrorNote, Field, ui } from "@/components/ui";
import styles from "./page.module.css";

export default function SignInPage() {
  const router = useRouter();
  const { user, loading } = useSession();
  const [mode, setMode] = useState<"signIn" | "register">("signIn");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loading && user) router.replace("/");
  }, [loading, user, router]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await (mode === "signIn" ? signIn(email, password) : register(email, password));
      router.replace("/");
    } catch (caught) {
      setError(describeAuthError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.intro}>
        <h1 className={styles.title}>{mode === "signIn" ? "Sign in" : "Create an account"}</h1>
        <p className={styles.lead}>
          Paste a posting, see each requirement matched against something you have actually done,
          and track where every application stands.
        </p>
      </div>

      <form className={styles.form} onSubmit={submit}>
        <Field label="Email" htmlFor="email">
          <input
            id="email"
            className={ui.input}
            type="email"
            value={email}
            autoComplete="email"
            required
            onChange={(event) => setEmail(event.target.value)}
          />
        </Field>

        <Field label="Password" htmlFor="password">
          <input
            id="password"
            className={ui.input}
            type="password"
            value={password}
            autoComplete={mode === "signIn" ? "current-password" : "new-password"}
            minLength={6}
            required
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>

        <ErrorNote>{error}</ErrorNote>

        <button className={ui.button} type="submit" disabled={busy}>
          {busy ? "Working…" : mode === "signIn" ? "Sign in" : "Create account"}
        </button>

        <button
          className={styles.switch}
          type="button"
          onClick={() => {
            setMode(mode === "signIn" ? "register" : "signIn");
            setError("");
          }}
        >
          {mode === "signIn" ? "No account yet? Create one." : "Already have an account? Sign in."}
        </button>
      </form>

      {isEmulated ? (
        <p className={styles.emulator}>
          Running against the local auth emulator on {config.authEmulatorHost}. Accounts created
          here exist only on this machine and are discarded when the emulator stops. Use any email
          and a password of six characters or more.
        </p>
      ) : null}
    </div>
  );
}
