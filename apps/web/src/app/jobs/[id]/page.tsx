"use client";

import { use, useCallback, useState } from "react";
import Link from "next/link";

import { api, ApiError, type ArtifactSummary, type JobDetail, type ScoreOut } from "@/lib/api";
import { useResource } from "@/lib/resource";
import { useRequireSession } from "@/lib/session";
import { CoverageChip, Empty, ErrorNote, Loading, Panel, ui } from "@/components/ui";
import styles from "./page.module.css";

export default function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { ready } = useRequireSession();
  const [busy, setBusy] = useState(false);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [tailoring, setTailoring] = useState(false);

  // Fetched together so the page never renders a score beside a posting it does not
  // describe — the two are one view, not two independent resources.
  const load = useCallback(async (): Promise<{
    job: JobDetail;
    score: ScoreOut | null;
    artifacts: ArtifactSummary[];
  }> => {
    const detail = await api.job(id);
    return {
      job: detail,
      score: detail.status === "ready" ? await api.score(id) : null,
      // Only an application has documents, so a posting the user is not tracking
      // simply has none rather than the page needing a second state for it.
      artifacts: detail.application_id ? await api.artifacts(detail.application_id) : [],
    };
  }, [id]);

  const { data, error, reload, setError } = useResource(load, {
    enabled: ready,
    fallback: "Could not load that posting.",
  });
  const job = data?.job ?? null;
  const score = data?.score ?? null;
  const documents = data?.artifacts ?? artifacts;

  async function generate() {
    if (!job?.application_id) return;
    setTailoring(true);
    try {
      await api.tailor(job.application_id);
      setArtifacts(await api.artifacts(job.application_id));
      await reload();
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "That document could not be generated.",
      );
    } finally {
      setTailoring(false);
    }
  }

  async function download(artifactId: string) {
    try {
      const url = await api.artifactPdf(artifactId);
      // A blob URL is not a file on disk, so it has to be opened before it is
      // revoked, and revoking it afterwards is what stops it leaking for the life of
      // the tab.
      window.open(url, "_blank", "noopener");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not open that PDF.");
    }
  }

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

      {score && (score.skills.have.length > 0 || score.skills.lack.length > 0) ? (
        <Panel
          title="Skills"
          note="Named technologies this posting asks for, matched exactly. A near miss is a miss here."
        >
          <div className={styles.skillColumns}>
            <div>
              <h3 className={styles.skillHeading}>
                You can show {score.skills.have.length} of{" "}
                {score.skills.have.length + score.skills.lack.length}
              </h3>
              <ul className={styles.skillList}>
                {score.skills.have.map((entry) => (
                  <li className={styles.skillHave} key={entry.skill}>
                    <span className={styles.skillName}>{entry.skill}</span>
                    <span className={styles.skillEvidence}>{entry.evidence}</span>
                  </li>
                ))}
              </ul>
            </div>

            {score.skills.lack.length > 0 ? (
              <div>
                <h3 className={styles.skillHeading}>Not in your profile</h3>
                <ul className={styles.skillList}>
                  {score.skills.lack.map((skill) => (
                    <li className={styles.skillLack} key={skill}>
                      {skill}
                    </li>
                  ))}
                </ul>
                <p className={styles.skillNote}>
                  If you have used one of these, add it to your profile — it is not counted until it
                  is written down somewhere.
                </p>
              </div>
            ) : null}
          </div>

          {score.skills.unused.length > 0 ? (
            <p className={styles.unused}>
              <strong>Not asked for here:</strong> {score.skills.unused.join(", ")}. Worth cutting
              from a resume tailored to this role.
            </p>
          ) : null}
        </Panel>
      ) : null}

      {job.application_id ? (
        <Panel
          title="Tailored resume"
          note="Built only from reviewed profile items. Every bullet cites the item it came from."
          action={
            <button
              className={ui.secondary}
              type="button"
              onClick={() => void generate()}
              disabled={tailoring}
            >
              {tailoring ? "Writing…" : documents.length > 0 ? "Generate again" : "Generate"}
            </button>
          }
        >
          {documents.length === 0 ? (
            <Empty>
              Nothing generated yet. A draft reorders your own lines for this posting and refuses
              anything it cannot trace back to one.
            </Empty>
          ) : (
            <ul className={styles.documents}>
              {documents.map((artifact) => (
                <li className={styles.document} key={artifact.id}>
                  <div>
                    <p className={styles.documentTitle}>
                      Version {artifact.version} · {artifact.bullet_count} bullets
                    </p>
                    {artifact.warnings.length > 0 ? (
                      <ul className={styles.warnings}>
                        {artifact.warnings.map((warning) => (
                          <li key={warning}>{warning}</li>
                        ))}
                      </ul>
                    ) : null}
                  </div>
                  <button
                    className={`${ui.secondary} ${ui.small}`}
                    type="button"
                    onClick={() => void download(artifact.id)}
                  >
                    Open PDF
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
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
