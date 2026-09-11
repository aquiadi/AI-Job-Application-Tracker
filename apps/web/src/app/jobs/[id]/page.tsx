"use client";

import { use, useCallback, useState } from "react";
import Link from "next/link";

import { api, ApiError, type JobDetail, type ScoreOut } from "@/lib/api";
import { useResource } from "@/lib/resource";
import { useRequireSession } from "@/lib/session";
import { CoverageChip, Empty, ErrorNote, Loading, Panel, ui } from "@/components/ui";
import styles from "./page.module.css";

export default function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { ready } = useRequireSession();
  const [busy, setBusy] = useState(false);

  // Fetched together so the page never renders a score beside a posting it does not
  // describe — the two are one view, not two independent resources.
  const load = useCallback(async (): Promise<{ job: JobDetail; score: ScoreOut | null }> => {
    const detail = await api.job(id);
    return { job: detail, score: detail.status === "ready" ? await api.score(id) : null };
  }, [id]);

  const { data, error, reload, setError } = useResource(load, {
    enabled: ready,
    fallback: "Could not load that posting.",
  });
  const job = data?.job ?? null;
  const score = data?.score ?? null;

  async function track() {
    if (!job) return;
    setBusy(true);
    try {
      await api.startApplication(job.id);
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not start tracking.");
    } finally {
      setBusy(false);
    }
  }

  if (!ready) return <Loading what="the posting" />;
  if (error && !job) return <ErrorNote>{error}</ErrorNote>;
  if (!job) return <Loading what="the posting" />;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <p className={styles.breadcrumb}>
          <Link href="/jobs">Postings</Link>
        </p>
        <h1 className={styles.title}>{job.title ?? "Untitled posting"}</h1>
        <p className={styles.meta}>
          {[job.company, job.location, job.seniority].filter(Boolean).join(" · ") || "—"}
        </p>
        <div className={styles.actions}>
          {job.source_url ? (
            <a className={styles.source} href={job.source_url} target="_blank" rel="noreferrer">
              Original posting
            </a>
          ) : null}
          {job.application_id ? (
            <Link className={styles.source} href="/">
              Tracking in pipeline
            </Link>
          ) : (
            <button
              className={ui.secondary}
              type="button"
              onClick={() => void track()}
              disabled={busy}
            >
              Track this
            </button>
          )}
        </div>
      </header>

      <ErrorNote>{error}</ErrorNote>

      {job.status !== "ready" ? (
        <Panel title="Still reading" note={`Status: ${job.status}`}>
          <Empty>
            {job.failure_reason ??
              "The posting is being read and indexed. This page updates when you reload."}
          </Empty>
        </Panel>
      ) : score ? (
        <>
          <section className={styles.scorePanel}>
            <div className={styles.scoreFigure}>
              <span className={styles.scoreValue}>{score.score}</span>
              <span className={styles.scoreOf}>/ 100</span>
            </div>
            <div className={styles.scoreText}>
              <h2 className={styles.scoreTitle}>Fit</h2>
              <p className={styles.scoreBreakdown}>
                {score.must_covered} of {score.must_total} must-haves covered
                {score.nice_total > 0
                  ? `, ${score.nice_covered} of ${score.nice_total} nice-to-haves`
                  : ""}
                .
              </p>
              <p className={styles.scoreNote}>
                Computed from the matches below, not written by a model. Each requirement is
                compared against your reviewed profile items; must-haves are weighted above
                nice-to-haves.
                {score.embedding_model === "heuristic" ? (
                  <>
                    {" "}
                    This run used the local backend, which matches on shared words rather than
                    meaning. Connect Vertex AI for semantic matching.
                  </>
                ) : null}
              </p>
            </div>
          </section>

          <Panel
            title="Requirements"
            note="Each one shown with the evidence that answered it, or marked as a gap."
          >
            {score.matches.length === 0 ? (
              <Empty>No requirements were found in this posting.</Empty>
            ) : (
              <ul className={styles.matches}>
                {score.matches.map((match) => (
                  <li className={styles.match} key={match.requirement_id}>
                    <CoverageChip coverage={match.coverage} />
                    <div className={styles.matchBody}>
                      <p className={styles.requirement}>
                        {match.requirement}
                        {match.kind === "nice" ? (
                          <span className={styles.kind}> nice to have</span>
                        ) : null}
                      </p>
                      {match.evidence ? (
                        <p className={styles.evidence}>{match.evidence}</p>
                      ) : (
                        <p className={styles.gap}>Nothing in your profile covers this.</p>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </>
      ) : null}

      {job.hard_skills.length > 0 ? (
        <Panel title="Named technologies" note="Taken from the posting, not inferred.">
          <ul className={styles.skills}>
            {job.hard_skills.map((skill) => (
              <li className={styles.skill} key={skill}>
                {skill}
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}

      {job.responsibilities.length > 0 ? (
        <Panel
          title="Responsibilities"
          note="What the role does, as distinct from what it asks for."
        >
          <ul className={styles.responsibilities}>
            {job.responsibilities.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </div>
  );
}
