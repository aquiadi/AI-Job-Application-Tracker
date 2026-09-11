"use client";

import { useCallback, useState } from "react";
import Link from "next/link";

import { api, ApiError, type ApplicationOut, type Board, type Stage } from "@/lib/api";
import { useResource } from "@/lib/resource";
import { useRequireSession } from "@/lib/session";
import { Empty, ErrorNote, Loading, ui } from "@/components/ui";
import styles from "./page.module.css";

const STAGE_LABELS: Record<string, string> = {
  saved: "Saved",
  applied: "Applied",
  screen: "Screen",
  technical: "Technical",
  onsite: "Onsite",
  offer: "Offer",
  rejected: "Rejected",
  withdrawn: "Withdrawn",
  ghosted: "Ghosted",
};

export default function PipelinePage() {
  const { ready } = useRequireSession();
  const [moving, setMoving] = useState<string | null>(null);
  const load = useCallback(() => api.board(), []);
  const {
    data: board,
    error,
    reload,
    setError,
  } = useResource<Board>(load, { enabled: ready, fallback: "Could not load the pipeline." });

  async function move(application: ApplicationOut, to: Stage) {
    setMoving(application.id);
    try {
      await api.moveStage(application.id, to);
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "That move was refused.");
    } finally {
      setMoving(null);
    }
  }

  if (!ready) return <Loading what="your pipeline" />;
  if (!board) return error ? <ErrorNote>{error}</ErrorNote> : <Loading what="your pipeline" />;

  const tracked = board.columns.reduce(
    (total, column) => total + column.applications.length,
    board.closed.length,
  );

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className={styles.title}>Pipeline</h1>
        <p className={styles.note}>
          {tracked === 0
            ? "Nothing tracked yet."
            : `${tracked} application${tracked === 1 ? "" : "s"}. Stage changes are recorded, so time in stage is measured rather than remembered.`}
        </p>
      </header>

      <ErrorNote>{error}</ErrorNote>

      {tracked === 0 ? (
        <Empty>
          Save a posting under <Link href="/jobs">Postings</Link>, then start tracking it to see it
          here.
        </Empty>
      ) : (
        <div className={styles.board}>
          {board.columns.map((column) => (
            <section className={styles.column} key={column.stage}>
              <h2 className={styles.columnTitle}>
                {STAGE_LABELS[column.stage] ?? column.stage}
                <span className={styles.count}>{column.applications.length}</span>
              </h2>
              <ul className={styles.cards}>
                {column.applications.map((application) => (
                  <li className={styles.card} key={application.id}>
                    <Link className={styles.cardTitle} href={`/jobs/${application.job_id}`}>
                      {application.title ?? "Untitled posting"}
                    </Link>
                    {application.company ? (
                      <p className={styles.company}>{application.company}</p>
                    ) : null}
                    <p className={styles.age}>
                      {application.days_in_stage === 0
                        ? "Today"
                        : `${application.days_in_stage}d in stage`}
                    </p>
                    <label className={styles.moveLabel} htmlFor={`move-${application.id}`}>
                      Move to
                    </label>
                    <select
                      id={`move-${application.id}`}
                      className={ui.select}
                      value=""
                      disabled={moving === application.id}
                      onChange={(event) => {
                        const next = event.target.value;
                        if (next) void move(application, next as Stage);
                      }}
                    >
                      <option value="">Choose…</option>
                      {application.allowed_next.map((stage) => (
                        <option key={stage} value={stage}>
                          {STAGE_LABELS[stage] ?? stage}
                        </option>
                      ))}
                    </select>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </div>
      )}

      {board.closed.length > 0 ? (
        <section className={styles.closed}>
          <h2 className={styles.closedTitle}>Closed</h2>
          <ul className={styles.closedList}>
            {board.closed.map((application) => (
              <li className={styles.closedRow} key={application.id}>
                <Link href={`/jobs/${application.job_id}`}>
                  {application.title ?? "Untitled posting"}
                </Link>
                <span className={styles.closedStage}>
                  {STAGE_LABELS[application.stage] ?? application.stage}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
