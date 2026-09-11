/**
 * The typed API client.
 *
 * Types come from `api-types.ts`, which `scripts/generate_client.sh` generates from
 * the API's own OpenAPI document. Hand-writing them would mean two descriptions of one
 * contract, and the first symptom of drift is a runtime shape error in a browser.
 */
"use client";

import { config } from "./config";
import { idToken } from "./auth";
import type { components, paths } from "./api-types";

type Schemas = components["schemas"];

export type Me = Schemas["Me"];
export type JobSummary = Schemas["JobSummary"];
export type JobDetail = Schemas["JobDetail"];
export type SavedJob = Schemas["SavedJob"];
export type SaveJob = Schemas["SaveJob"];
export type ScoreOut = Schemas["ScoreOut"];
export type MatchOut = Schemas["MatchOut"];
export type ProfileOut = Schemas["ProfileOut"];
export type ItemOut = Schemas["ItemOut"];
export type ItemIn = Schemas["ItemIn"];
export type ItemPatch = Schemas["ItemPatch"];
export type ProfileUpdate = Schemas["ProfileUpdate"];
export type ImportOut = Schemas["ImportOut"];
export type Board = Schemas["Board"];
export type ApplicationOut = Schemas["ApplicationOut"];
export type ApplicationDetail = Schemas["ApplicationDetail"];
export type Stage = Schemas["Stage"];
export type Coverage = Schemas["Coverage"];
export type JobStatus = Schemas["JobStatus"];
export type ProfileItemKind = Schemas["ProfileItemKind"];
export type RequirementKind = Schemas["RequirementKind"];

/** A non-2xx response, carrying the API's own `code` and `detail`. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await idToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${config.apiUrl}${path}`, { ...init, headers });
  } catch {
    // A failed fetch is indistinguishable from a CORS rejection in the browser, and
    // both mean the same thing to the person reading the screen.
    throw new ApiError(0, "unreachable", "Could not reach the API. Is it running?");
  }

  if (response.status === 204) return undefined as T;

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body as { code?: string; detail?: string } | null;
    throw new ApiError(
      response.status,
      detail?.code ?? "error",
      detail?.detail ?? `Request failed (${response.status})`,
    );
  }
  return body as T;
}

export const api = {
  me: () => request<Me>("/me"),

  profile: () => request<ProfileOut>("/profile"),
  updateProfile: (body: ProfileUpdate) =>
    request<ProfileOut>("/profile", { method: "PATCH", body: JSON.stringify(body) }),
  addItem: (body: ItemIn) =>
    request<ItemOut>("/profile/items", { method: "POST", body: JSON.stringify(body) }),
  updateItem: (id: string, body: ItemPatch) =>
    request<ItemOut>(`/profile/items/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteItem: (id: string) => request<void>(`/profile/items/${id}`, { method: "DELETE" }),
  reviewAll: () => request<ProfileOut>("/profile/items/review-all", { method: "POST" }),
  importResume: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ImportOut>("/profile/import", { method: "POST", body: form });
  },

  jobs: () => request<JobSummary[]>("/jobs"),
  job: (id: string) => request<JobDetail>(`/jobs/${id}`),
  // Typed from the generated request schema rather than restated here, so a field
  // added to the endpoint cannot go missing from the client without a type error.
  saveJob: (body: Schemas["SaveJob"]) =>
    request<SavedJob>("/jobs", { method: "POST", body: JSON.stringify(body) }),
  retryJob: (id: string) => request<SavedJob>(`/jobs/${id}/retry`, { method: "POST" }),
  score: (id: string) => request<ScoreOut>(`/jobs/${id}/score`),

  board: () => request<Board>("/applications"),
  application: (id: string) => request<ApplicationDetail>(`/applications/${id}`),
  startApplication: (jobId: string) =>
    request<ApplicationDetail>("/applications", {
      method: "POST",
      body: JSON.stringify({ job_id: jobId }),
    }),
  moveStage: (id: string, toStage: Stage, note?: string) =>
    request<ApplicationDetail>(`/applications/${id}/stage`, {
      method: "POST",
      body: JSON.stringify({ to_stage: toStage, note: note ?? null }),
    }),
  stopTracking: (id: string) => request<void>(`/applications/${id}`, { method: "DELETE" }),
};

export type ApiPaths = paths;
