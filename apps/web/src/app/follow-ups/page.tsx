"use client";

import { useCallback, useState } from "react";

import { api, ApiError, type NudgeOut } from "@/lib/api";
import { useResource } from "@/lib/resource";
import { useRequireSession } from "@/lib/session";
import { Empty, ErrorNote, Loading, Panel, ui } from "@/components/ui";
import styles from "./page.module.css";

const STAGE_LABELS: Record<string, string> = {
  applied: "Applied",
  screen: "Screen",
  technical: "Technical",
  onsite: "Onsite",
  offer: "Offer",
};

export default function FollowUpsPage() {
  const { ready } = useRequireSession();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [copied, setCopied] = useState<string | null>(null);

  const load = useCallback(() => api.nudges(), []);
  const {
    data: nudges,
    error,
    reload,
    setError,
  } = useResource<NudgeOut[]>(load, {
    enabled: ready,
    fallback: "Could not load your follow-ups.",
  });

  async function run(work: () => Promise<unknown>, failure: string) {
    setBusy(true);
    setError("");
    try {
      await work();
      await reload();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : failure);
    } finally {
      setBusy(false);
    }
  }

  async function sweep() {
    await run(async () => {
      const result = await api.sweep();
      setNotice(
        result.drafted > 0
          ? `Drafted ${result.drafted} follow-up${result.drafted === 1 ? "" : "s"}.`
          : result.considered > 0
            ? "Everything that has gone quiet already has a draft."
            : "Nothing has been waiting long enough to chase.",
      );
    }, "The sweep could not run.");
  }

  async function copy(nudge: NudgeOut) {
    const text = `Subject: ${nudge.draft_subject ?? ""}\n\n${nudge.draft_body ?? ""}`;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(nudge.id);
      setTimeout(() => setCopied(null), 2500);
    } catch {
      // Clipboard access is denied in some browsers without a user gesture chain, and
      // failing silently would look like the button does nothing.
      setError("Your browser would not let the page copy. Select the text instead.");
    }
  }

  if (!ready) return <Loading what="your follow-ups" />;

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className={styles.title}>Follow-ups</h1>
        <p className={styles.note}>
          Drafts for applications that have gone quiet. Nothing here has been sent, and nothing here
          can be: you copy the text and send it yourself, from your own address.
        </p>
      </header>

      <ErrorNote>{error}</ErrorNote>
      {notice ? <p className={styles.notice}>{notice}</p> : null}

      <Panel
        title="Drafts"
        note={nudges ? `${nudges.length} waiting` : undefined}
        action={
          <button
            className={ui.secondary}
            type="button"
            onClick={() => void sweep()}
            disabled={busy}
          >
            Check for stalled applications
          </button>
        }
      >
        {!nudges ? (
          <Loading what="drafts" />
        ) : nudges.length === 0 ? (
          <Empty>
            Nothing to chase. Thresholds are in working days — ten after applying, five after an
            interview stage, three after an offer — so a Friday application is not stale on Monday.
          </Empty>
        ) : (
          <ul className={styles.list}>
            {nudges.map((nudge) => (
              <li className={styles.item} key={nudge.id}>
                <div className={styles.itemHead}>
                  <div>
                    <p className={styles.role}>{nudge.title ?? "Untitled posting"}</p>
                    <p className={styles.meta}>
                      {[nudge.company, STAGE_LABELS[nudge.stage] ?? nudge.stage]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  </div>
                  <span className={styles.state}>{nudge.state}</span>
                </div>

                <p className={styles.subject}>{nudge.draft_subject}</p>

                {editing === nudge.id ? (
                  <>
                    <textarea
                      className={ui.textarea}
                      value={draft}
                      aria-label="Edit the draft"
                      onChange={(event) => setDraft(event.target.value)}
                    />
                    <div className={styles.actions}>
                      <button
                        className={`${ui.button} ${ui.small}`}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void run(async () => {
                            await api.updateNudge(nudge.id, { draft_body: draft });
                            setEditing(null);
                          }, "That edit could not be saved.")
                        }
                      >
                        Save
                      </button>
                      <button
                        className={`${ui.secondary} ${ui.small}`}
                        type="button"
                        onClick={() => setEditing(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    <p className={styles.body}>{nudge.draft_body}</p>
                    <div className={styles.actions}>
                      <button
                        className={`${ui.secondary} ${ui.small}`}
                        type="button"
                        onClick={() => void copy(nudge)}
                      >
                        {copied === nudge.id ? "Copied" : "Copy"}
                      </button>
                      <button
                        className={`${ui.secondary} ${ui.small}`}
                        type="button"
                        onClick={() => {
                          setEditing(nudge.id);
                          setDraft(nudge.draft_body ?? "");
                        }}
                      >
                        Edit
                      </button>
                      <button
                        className={`${ui.secondary} ${ui.small}`}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void run(
                            () => api.updateNudge(nudge.id, { state: "approved", outcome: "sent" }),
                            "Could not record that.",
                          )
                        }
                      >
                        I sent this
                      </button>
                      <button
                        className={`${ui.danger} ${ui.small}`}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void run(
                            () => api.updateNudge(nudge.id, { state: "dismissed" }),
                            "Could not dismiss that.",
                          )
                        }
                      >
                        Dismiss
                      </button>
                    </div>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
