"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import Link from "next/link";

import { api, ApiError, type JobSummary } from "@/lib/api";
import { useResource } from "@/lib/resource";
import { useRequireSession } from "@/lib/session";
import { Empty, ErrorNote, Loading, Panel, ui } from "@/components/ui";
import styles from "./page.module.css";

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  extracting: "Reading",
  embedding: "Indexing",
  ready: "Ready",
  failed: "Failed",
};

/** Statuses that will change on their own, so the list is worth re-fetching. */
const IN_FLIGHT = new Set(["queued", "extracting", "embedding"]);

export default function JobsPage() {
  const { ready } = useRequireSession();
  const [mode, setMode] = useState<"url" | "text">("url");
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => api.jobs(), []);
  const {
    data: jobs,
    error,
    reload,
    setError,
  } = useResource<JobSummary[]>(load, {
    enabled: ready,
    fallback: "Could not load your postings.",
  });

  // Extraction runs behind the outbox, so a posting saved a second ago is not
  // finished yet. Polling only while something is actually in flight keeps a quiet
  // page quiet.
  useEffect(() => {
    if (!jobs?.some((job) => IN_FLIGHT.has(job.status))) return;
    timer.current = setTimeout(() => void reload(), 1500);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [jobs, reload]);

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const saved = await api.saveJob(
        mode === "url"
          ? { url, text: null, title: null, company: null }
          : { url: null, text, title: title || null, company: company || null },
      );
      setNotice(
        saved.created ? "Saved. Reading the posting now." : "You had already saved that one.",
      );
      setUrl("");
      setText("");
      setTitle("");
      setCompany("");
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function track(job: JobSummary) {
    try {
      await api.startApplication(job.id);
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not start tracking.");
    }
  }

  if (!ready) return <Loading what="your postings" />;

  return (
    <div className={styles.page}>
      <Panel
        title="Save a posting"
        note="A Greenhouse or Lever link, or the description text from anywhere else."
      >
        <form className={styles.form} onSubmit={save}>
          <div className={styles.modes} role="group" aria-label="How to add the posting">
            <button
              type="button"
              className={mode === "url" ? styles.modeActive : styles.mode}
              onClick={() => setMode("url")}
              aria-pressed={mode === "url"}
            >
              Link
            </button>
            <button
              type="button"
              className={mode === "text" ? styles.modeActive : styles.mode}
              onClick={() => setMode("text")}
              aria-pressed={mode === "text"}
            >
              Paste text
            </button>
          </div>

          {mode === "url" ? (
            <input
              className={ui.input}
              type="url"
              value={url}
              required
              placeholder="https://job-boards.greenhouse.io/company/jobs/1234567"
              aria-label="Posting link"
              onChange={(event) => setUrl(event.target.value)}
            />
          ) : (
            <>
              <textarea
                className={ui.textarea}
                value={text}
                required
                minLength={120}
                placeholder="Paste the full job description."
                aria-label="Posting text"
                onChange={(event) => setText(event.target.value)}
              />
              {/* A job board states the role and the employer as structured fields.
                  A paste does not, and inferring them from the first line is wrong
                  often enough to be worse than asking — so this asks, and accepts
                  nothing rather than guessing. */}
              <div className={styles.pair}>
                <input
                  className={ui.input}
                  value={title}
                  placeholder="Role title (optional)"
                  aria-label="Role title"
                  maxLength={255}
                  onChange={(event) => setTitle(event.target.value)}
                />
                <input
                  className={ui.input}
                  value={company}
                  placeholder="Company (optional)"
                  aria-label="Company"
                  maxLength={255}
                  onChange={(event) => setCompany(event.target.value)}
                />
              </div>
            </>
          )}

          <ErrorNote>{error}</ErrorNote>
          {notice ? <p className={styles.notice}>{notice}</p> : null}

          <button className={ui.button} type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save posting"}
          </button>
        </form>
      </Panel>

      <Panel
        title="Saved"
        note={jobs ? `${jobs.length} posting${jobs.length === 1 ? "" : "s"}` : undefined}
      >
        {!jobs ? (
          <Loading what="postings" />
        ) : jobs.length === 0 ? (
          <Empty>Nothing saved yet. Add a posting above and it will appear here.</Empty>
        ) : (
          <ul className={styles.list}>
            {jobs.map((job) => (
              <li className={styles.row} key={job.id}>
                <div className={styles.rowMain}>
                  <Link className={styles.rowTitle} href={`/jobs/${job.id}`}>
                    {job.title ?? "Untitled posting"}
                  </Link>
                  <p className={styles.rowMeta}>
                    {[job.company, job.location].filter(Boolean).join(" · ") || "—"}
                  </p>
                  {job.failure_reason ? (
                    <p className={styles.failure}>{job.failure_reason}</p>
                  ) : null}
                </div>

                <div className={styles.rowSide}>
                  <span className={styles.status} data-status={job.status}>
                    {STATUS_LABELS[job.status] ?? job.status}
                  </span>
                  <span className={styles.requirements}>
                    {job.requirement_count} requirement{job.requirement_count === 1 ? "" : "s"}
                  </span>
                  {job.status === "failed" ? (
                    <button
                      className={`${ui.secondary} ${ui.small}`}
                      type="button"
                      onClick={() => void api.retryJob(job.id).then(reload)}
                    >
                      Try again
                    </button>
                  ) : job.application_id ? (
                    <span className={styles.tracked}>Tracking</span>
                  ) : (
                    <button
                      className={`${ui.secondary} ${ui.small}`}
                      type="button"
                      onClick={() => void track(job)}
                    >
                      Track
                    </button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
