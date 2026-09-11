"use client";

import { useCallback, useState, type ChangeEvent, type FormEvent } from "react";

import { api, ApiError, type ItemOut, type ProfileItemKind, type ProfileOut } from "@/lib/api";
import { useResource } from "@/lib/resource";
import { useRequireSession } from "@/lib/session";
import { Empty, ErrorNote, Field, Loading, Panel, ui } from "@/components/ui";
import styles from "./page.module.css";

const KINDS: { value: ProfileItemKind; label: string }[] = [
  { value: "experience_bullet", label: "Experience" },
  { value: "project", label: "Project" },
  { value: "skill", label: "Skill" },
  { value: "education", label: "Education" },
];

export default function ProfilePage() {
  const { ready } = useRequireSession();
  const [notice, setNotice] = useState("");
  const [draft, setDraft] = useState("");
  const [draftKind, setDraftKind] = useState<ProfileItemKind>("experience_bullet");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [editText, setEditText] = useState("");

  const load = useCallback(() => api.profile(), []);
  const {
    data: profile,
    error,
    reload,
    setError,
  } = useResource<ProfileOut>(load, {
    enabled: ready,
    fallback: "Could not load your profile.",
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

  async function addItem(event: FormEvent) {
    event.preventDefault();
    await run(async () => {
      await api.addItem({ text: draft, kind: draftKind });
      setDraft("");
    }, "That item could not be added.");
  }

  async function saveContact(event: FormEvent) {
    event.preventDefault();
    const form = new FormData(event.target as HTMLFormElement);
    await run(async () => {
      await api.updateProfile({
        full_name: String(form.get("full_name") ?? "") || null,
        contact_email: String(form.get("contact_email") ?? "") || null,
        phone: String(form.get("phone") ?? "") || null,
        location: String(form.get("location") ?? "") || null,
        headline: String(form.get("headline") ?? "") || null,
      });
      setNotice("Contact details saved.");
    }, "Those details could not be saved.");
  }

  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    setNotice("");
    await run(async () => {
      const result = await api.importResume(file);
      setNotice(
        `Imported ${result.imported} item${result.imported === 1 ? "" : "s"}. Review them below — nothing counts towards a score until you do.`,
      );
    }, "That resume could not be imported.");
    event.target.value = "";
  }

  if (!ready) return <Loading what="your profile" />;
  if (!profile) return error ? <ErrorNote>{error}</ErrorNote> : <Loading what="your profile" />;

  const unreviewed = profile.items.filter((item) => !item.reviewed);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className={styles.title}>Profile</h1>
        <p className={styles.note}>
          Everything a fit score is computed from, and everything a generated document may cite.{" "}
          {profile.reviewed_count} reviewed
          {profile.unreviewed_count > 0 ? `, ${profile.unreviewed_count} awaiting review` : ""}.
        </p>
      </header>

      <ErrorNote>{error}</ErrorNote>
      {notice ? <p className={styles.notice}>{notice}</p> : null}

      <Panel
        title="Contact details"
        note="Re-attached when a document is rendered. Never sent to a model."
      >
        <form className={styles.contact} onSubmit={saveContact}>
          <Field label="Full name" htmlFor="full_name">
            <input
              id="full_name"
              name="full_name"
              className={ui.input}
              defaultValue={profile.full_name ?? ""}
            />
          </Field>
          <Field label="Email" htmlFor="contact_email">
            <input
              id="contact_email"
              name="contact_email"
              type="email"
              className={ui.input}
              defaultValue={profile.contact_email ?? ""}
            />
          </Field>
          <Field label="Phone" htmlFor="phone">
            <input
              id="phone"
              name="phone"
              className={ui.input}
              defaultValue={profile.phone ?? ""}
            />
          </Field>
          <Field label="Location" htmlFor="location">
            <input
              id="location"
              name="location"
              className={ui.input}
              defaultValue={profile.location ?? ""}
            />
          </Field>
          <div className={styles.wide}>
            <Field label="Headline" htmlFor="headline">
              <input
                id="headline"
                name="headline"
                className={ui.input}
                defaultValue={profile.headline ?? ""}
              />
            </Field>
          </div>
          <button className={ui.button} type="submit" disabled={busy}>
            Save details
          </button>
        </form>
      </Panel>

      <Panel
        title="Import a resume"
        note="A PDF with a text layer. Everything it finds arrives unreviewed."
      >
        <label className={styles.upload}>
          <input type="file" accept="application/pdf" onChange={upload} disabled={busy} />
        </label>
      </Panel>

      <Panel
        title="Evidence"
        note="One item per thing you have done. Kept separate because each is compared against a requirement on its own."
        action={
          unreviewed.length > 0 ? (
            <button
              className={`${ui.secondary} ${ui.small}`}
              type="button"
              disabled={busy}
              onClick={() => void run(() => api.reviewAll(), "Could not accept those items.")}
            >
              Accept all {unreviewed.length}
            </button>
          ) : undefined
        }
      >
        <form className={styles.add} onSubmit={addItem}>
          <textarea
            className={ui.textarea}
            value={draft}
            required
            minLength={3}
            rows={3}
            placeholder="Designed the idempotent ledger write path behind 40M daily postings"
            aria-label="New item"
            onChange={(event) => setDraft(event.target.value)}
          />
          <div className={styles.addControls}>
            <select
              className={ui.select}
              value={draftKind}
              aria-label="Kind"
              onChange={(event) => setDraftKind(event.target.value as ProfileItemKind)}
            >
              {KINDS.map((kind) => (
                <option key={kind.value} value={kind.value}>
                  {kind.label}
                </option>
              ))}
            </select>
            <button className={ui.button} type="submit" disabled={busy}>
              Add item
            </button>
          </div>
        </form>

        {profile.items.length === 0 ? (
          <Empty>
            Nothing here yet. Add an item above, or import a resume — a score needs something to
            compare against.
          </Empty>
        ) : (
          <ul className={styles.items}>
            {profile.items.map((item: ItemOut) => (
              <li className={styles.item} key={item.id} data-reviewed={item.reviewed}>
                {editing === item.id ? (
                  <div className={styles.editing}>
                    <textarea
                      className={ui.textarea}
                      value={editText}
                      rows={3}
                      aria-label="Edit item"
                      onChange={(event) => setEditText(event.target.value)}
                    />
                    <div className={styles.editControls}>
                      <button
                        className={`${ui.button} ${ui.small}`}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void run(async () => {
                            await api.updateItem(item.id, { text: editText, reviewed: true });
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
                  </div>
                ) : (
                  <>
                    <p className={styles.itemText}>{item.text}</p>
                    <div className={styles.itemMeta}>
                      <span className={styles.itemKind}>
                        {KINDS.find((kind) => kind.value === item.kind)?.label ?? item.kind}
                      </span>
                      {!item.reviewed ? (
                        <span className={styles.needsReview}>Needs review</span>
                      ) : null}
                      {!item.embedded ? <span className={styles.pending}>Indexing</span> : null}
                      <button
                        className={styles.link}
                        type="button"
                        onClick={() => {
                          setEditing(item.id);
                          setEditText(item.text);
                        }}
                      >
                        Edit
                      </button>
                      {!item.reviewed ? (
                        <button
                          className={styles.link}
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void run(
                              () => api.updateItem(item.id, { reviewed: true }),
                              "Could not accept that item.",
                            )
                          }
                        >
                          Accept
                        </button>
                      ) : null}
                      <button
                        className={styles.linkDanger}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void run(() => api.deleteItem(item.id), "Could not remove that item.")
                        }
                      >
                        Remove
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
