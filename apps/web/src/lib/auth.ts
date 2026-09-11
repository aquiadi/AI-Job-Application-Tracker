/**
 * Sign-in, and getting a token onto every request.
 *
 * Identity Platform in cloud, its emulator locally. The two differ in exactly one
 * place — `connectAuthEmulator` — and nothing downstream knows which is in use.
 *
 * Email and password rather than a social provider, because a social provider needs
 * OAuth client configuration in a project that does not have billing yet, and the
 * point of the local stack is that it needs nothing.
 */
"use client";

import { initializeApp, getApps, type FirebaseApp } from "firebase/app";
import {
  connectAuthEmulator,
  createUserWithEmailAndPassword,
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
  type Auth,
  type User,
} from "firebase/auth";

import { config, isEmulated } from "./config";

let cached: Auth | null = null;

function app(): FirebaseApp {
  const [existing] = getApps();
  if (existing) return existing;
  return initializeApp({
    apiKey: config.apiKey,
    authDomain: `${config.projectId}.firebaseapp.com`,
    projectId: config.projectId,
  });
}

export function auth(): Auth {
  if (cached) return cached;
  const instance = getAuth(app());
  if (isEmulated) {
    // `disableWarnings` only suppresses the banner the SDK prints; the emulator is
    // still obvious from the sign-in screen, which says so.
    connectAuthEmulator(instance, `http://${config.authEmulatorHost}`, {
      disableWarnings: true,
    });
  }
  cached = instance;
  return instance;
}

export async function signIn(email: string, password: string): Promise<void> {
  await signInWithEmailAndPassword(auth(), email, password);
}

export async function register(email: string, password: string): Promise<void> {
  await createUserWithEmailAndPassword(auth(), email, password);
}

export async function signOut(): Promise<void> {
  await firebaseSignOut(auth());
}

export function watchUser(handler: (user: User | null) => void): () => void {
  return onAuthStateChanged(auth(), handler);
}

/**
 * The current ID token, refreshed by the SDK when it is close to expiry.
 *
 * Read per request rather than held in a variable. A token lives an hour, a tab lives
 * longer, and a cached one produces a 401 that looks like a bug in the API.
 */
export async function idToken(): Promise<string | null> {
  const user = auth().currentUser;
  return user ? user.getIdToken() : null;
}

/** Turn Firebase's error codes into something worth showing a person. */
export function describeAuthError(error: unknown): string {
  const code = (error as { code?: string })?.code ?? "";
  switch (code) {
    case "auth/invalid-credential":
    case "auth/wrong-password":
    case "auth/user-not-found":
      return "That email and password do not match an account.";
    case "auth/email-already-in-use":
      return "There is already an account with that email. Sign in instead.";
    case "auth/weak-password":
      return "Passwords need at least six characters.";
    case "auth/invalid-email":
      return "That does not look like an email address.";
    case "auth/network-request-failed":
      return "Could not reach the sign-in service. Is the local stack running?";
    default:
      return "Sign-in failed. Try again.";
  }
}
