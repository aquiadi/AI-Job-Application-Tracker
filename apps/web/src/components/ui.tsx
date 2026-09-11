"use client";

import type { ReactNode } from "react";

import styles from "./ui.module.css";

export function Panel({
  title,
  note,
  action,
  children,
}: {
  title: string;
  note?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <h2 className={styles.panelTitle}>{title}</h2>
          {note ? <p className={styles.panelNote}>{note}</p> : null}
        </div>
        {action}
      </div>
      <div className={styles.panelBody}>{children}</div>
    </section>
  );
}

export function Field({
  label,
  htmlFor,
  children,
}: {
  label: string;
  htmlFor: string;
  children: ReactNode;
}) {
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={htmlFor}>
        {label}
      </label>
      {children}
    </div>
  );
}

export function CoverageChip({ coverage }: { coverage: string }) {
  const label = coverage === "covered" ? "Covered" : coverage === "partial" ? "Partial" : "Missing";
  return (
    <span className={styles.chip} data-coverage={coverage}>
      {label}
    </span>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return children ? (
    <p className={styles.error} role="alert">
      {children}
    </p>
  ) : null;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className={styles.empty}>{children}</p>;
}

export function Loading({ what }: { what: string }) {
  return (
    <p className={styles.spinner} role="status">
      Loading {what}…
    </p>
  );
}

export { styles as ui };
