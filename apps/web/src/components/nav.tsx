"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useSession } from "@/lib/session";
import styles from "./nav.module.css";

const LINKS = [
  { href: "/", label: "Pipeline" },
  { href: "/jobs", label: "Postings" },
  { href: "/profile", label: "Profile" },
] as const;

export function Nav() {
  const pathname = usePathname();
  const { user, signOut } = useSession();

  if (!user) return null;

  return (
    <nav className={styles.nav} aria-label="Main">
      <ul className={styles.links}>
        {LINKS.map((link) => {
          const active = link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
          return (
            <li key={link.href}>
              <Link
                className={styles.link}
                href={link.href}
                aria-current={active ? "page" : undefined}
              >
                {link.label}
              </Link>
            </li>
          );
        })}
      </ul>
      <div className={styles.account}>
        <span className={styles.email}>{user.email}</span>
        <button className={styles.signOut} type="button" onClick={() => void signOut()}>
          Sign out
        </button>
      </div>
    </nav>
  );
}
