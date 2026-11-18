import Link from "next/link";
import type { ReactNode } from "react";
import { Logo } from "./Logo";

type NavItem = { label: string; href?: string; phase?: string };

// Mirrors the nav in docs/design/screens/01-projects. Screens not built yet show their phase.
const PRIMARY: NavItem[] = [
  { label: "Projects", href: "/projects" },
  { label: "Panel reader", phase: "Phase 1" },
  { label: "Series bible", phase: "Phase 1" },
  { label: "Voice casting", phase: "Phase 1" },
  { label: "Art direction", phase: "Phase 2" },
  { label: "Director studio", phase: "Phase 2" },
];
const SECONDARY: NavItem[] = [
  { label: "Review queue", phase: "Phase 1" },
  { label: "Render jobs", phase: "Phase 1" },
  { label: "Models & licenses", phase: "Phase 1" },
];

const itemBase = "flex min-h-touch items-center justify-between rounded-md px-3 text-[15px]";

function Item({ item, current }: { item: NavItem; current: string }) {
  if (!item.href) {
    return (
      <span className={`${itemBase} text-text-muted`} aria-disabled="true">
        {item.label}
        <span className="text-xs">{item.phase}</span>
      </span>
    );
  }
  const active = current === item.href;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      className={`${itemBase} no-underline ${active ? "bg-surface-raised font-semibold" : "text-text-secondary"}`}
    >
      {item.label}
    </Link>
  );
}

export function AppShell({
  current,
  profile,
  children,
}: {
  current: string;
  profile: string;
  children: ReactNode;
}) {
  return (
    <div className="flex min-h-screen flex-wrap bg-night text-text">
      <nav
        aria-label="Main"
        className="flex shrink grow basis-nav flex-col gap-1.5 border-r border-border bg-ink px-4 py-6"
      >
        <Logo />
        {PRIMARY.map((i) => (
          <Item key={i.label} item={i} current={current} />
        ))}
        <div className="mx-1 my-3 h-px bg-border" role="separator" />
        {SECONDARY.map((i) => (
          <Item key={i.label} item={i} current={current} />
        ))}
        <div className="mt-auto flex flex-col gap-1.5 rounded-lg border border-border p-3.5 text-[13px]">
          <div className="text-text-muted">Compute</div>
          <div className="flex justify-between">
            <span>Profile</span>
            <span className="text-status-pass">{profile}</span>
          </div>
          <div className="flex justify-between">
            <span>Local GPU</span>
            <span className="text-text-muted">{profile === "gpu" ? "enabled" : "not used"}</span>
          </div>
        </div>
      </nav>
      <main className="flex min-w-0 shrink grow-[999] basis-[560px] flex-col gap-7 px-5 py-9 sm:px-11">
        {children}
      </main>
    </div>
  );
}
