import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { IBM_Plex_Sans, Spectral } from "next/font/google";

import "@/styles/tokens.css";
import "@/styles/base.css";
import styles from "./layout.module.css";

// next/font downloads and serves these from our own origin at build time, so there
// is no request to a Google domain from a user's browser and no layout shift while
// a webfont arrives.
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-plex-sans",
  display: "swap",
});

const spectral = Spectral({
  subsets: ["latin"],
  weight: ["500", "600"],
  variable: "--font-spectral",
  display: "swap",
});

export const metadata: Metadata = {
  title: {
    default: "Job Application Tracker",
    template: "%s · Job Application Tracker",
  },
  description:
    "Turn a job posting into a scored, evidence-backed application you can actually send.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f2f4f3" },
    { media: "(prefers-color-scheme: dark)", color: "#0f1716" },
  ],
};

// Typed explicitly rather than with Next's generated `LayoutProps`, which only
// exists after a build has written .next/types. `make check` runs the typecheck
// before any build, and a gate that needs a prior build is not a gate.
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${plexSans.variable} ${spectral.variable}`}>
      <body>
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <div className={styles.shell}>
          <header className={styles.header}>
            <span className={styles.wordmark}>Job Application Tracker</span>
          </header>
          <main id="main" className={styles.main}>
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
