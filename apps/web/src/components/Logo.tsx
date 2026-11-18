/** Kagebox mark + wordmark (from docs/design/logo/kagebox-mark-small.svg). */
export function Logo() {
  return (
    <div className="flex items-center gap-2.5 px-2.5 pt-1 pb-5">
      <svg viewBox="0 0 120 120" width="30" height="30" aria-hidden="true">
        <rect x="26" y="26" width="84" height="84" rx="14" className="fill-brand-vermilion" />
        <rect x="13" y="13" width="84" height="84" rx="14" className="fill-paper" />
        <path d="M44 34 L44 76 L80 55 Z" className="fill-ink" />
      </svg>
      <span className="font-display text-[21px] font-bold tracking-[-0.04em]">kagebox</span>
    </div>
  );
}
