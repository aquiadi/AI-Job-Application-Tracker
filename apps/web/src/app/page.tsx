import styles from "./page.module.css";

type Coverage = "covered" | "partial" | "missing";

interface ExampleRequirement {
  readonly requirement: string;
  readonly kind: "must" | "nice";
  readonly coverage: Coverage;
  readonly evidence: string | null;
}

/**
 * A worked example, not live data. It exists so the first thing on the page is the
 * thing the product is actually for: a posting broken into requirements, each one
 * answered by a line from your own history or openly marked as unanswered.
 */
const EXAMPLE: readonly ExampleRequirement[] = [
  {
    requirement: "5+ years building backend services in Python or Go",
    kind: "must",
    coverage: "covered",
    evidence: "Six years on payment settlement services in Python at Northwind",
  },
  {
    requirement: "Distributed transactions and idempotent processing",
    kind: "must",
    coverage: "covered",
    evidence: "Designed the idempotent ledger write path behind 40M daily postings",
  },
  {
    requirement: "Operating services on Kubernetes in production",
    kind: "must",
    coverage: "partial",
    evidence: "Deployed to GKE and carried the on-call pager; no cluster ownership",
  },
  {
    requirement: "Event streaming with Kafka or Pub/Sub",
    kind: "nice",
    coverage: "partial",
    evidence: "Consumed Pub/Sub topics for reconciliation jobs",
  },
  {
    requirement: "Authored reusable Terraform modules",
    kind: "nice",
    coverage: "missing",
    evidence: null,
  },
];

const COVERAGE_LABEL: Record<Coverage, string> = {
  covered: "Covered",
  partial: "Partial",
  missing: "Missing",
};

export default function Home() {
  return (
    <div className={styles.page}>
      <section className={styles.intro}>
        <h1 className={styles.title}>Every requirement, answered by something you have done</h1>
        <p className={styles.lead}>
          Paste a posting. It is split into requirements, each requirement is matched against
          evidence in your profile, and the fit score is computed from those matches rather than
          guessed by a model. A tailored draft can only reuse lines that already exist in your
          history, so it cannot invent a skill you do not have.
        </p>
      </section>

      <section className={styles.example} aria-labelledby="example-heading">
        <div className={styles.exampleHead}>
          <h2 id="example-heading" className={styles.exampleTitle}>
            Senior Backend Engineer, Payments
          </h2>
          <p className={styles.exampleNote}>
            A worked example. Your own breakdown is built from your profile.
          </p>
        </div>

        <ul className={styles.requirements} role="list">
          {EXAMPLE.map((item) => (
            <li key={item.requirement} className={styles.requirement}>
              <span className={styles.chip} data-coverage={item.coverage}>
                {COVERAGE_LABEL[item.coverage]}
              </span>
              <div className={styles.requirementBody}>
                <p className={styles.requirementText}>
                  {item.requirement}
                  {item.kind === "nice" ? <span className={styles.kind}> nice to have</span> : null}
                </p>
                {item.evidence === null ? (
                  <p className={styles.noEvidence}>Nothing in your profile covers this.</p>
                ) : (
                  <p className={styles.evidence}>{item.evidence}</p>
                )}
              </div>
            </li>
          ))}
        </ul>
      </section>

      <section className={styles.principles} aria-labelledby="principles-heading">
        <h2 id="principles-heading" className={styles.principlesTitle}>
          Three things this does differently
        </h2>
        <dl className={styles.principleList}>
          <div className={styles.principle}>
            <dt>The score is computed</dt>
            <dd>
              Each requirement is compared against your profile items by embedding similarity, and
              the score is the weighted coverage of those comparisons. A model never produces the
              number, so the same inputs always give the same score and every point of it traces to
              a specific line.
            </dd>
          </div>
          <div className={styles.principle}>
            <dt>Nothing is fabricated</dt>
            <dd>
              Every generated bullet cites the profile items it came from. A validator rejects any
              bullet that cites nothing, cites something that does not exist, or introduces a skill
              or a number absent from what it cited. Rejected bullets are regenerated once, then
              dropped with a warning rather than kept.
            </dd>
          </div>
          <div className={styles.principle}>
            <dt>Nothing is sent for you</dt>
            <dd>
              Follow-ups are drafted when an application has sat in a stage too long, and they stay
              drafts. You edit them, you decide, and you send from your own mail client.
            </dd>
          </div>
        </dl>
      </section>
    </div>
  );
}
