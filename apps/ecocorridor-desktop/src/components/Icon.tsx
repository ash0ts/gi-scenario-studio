import type { ReactNode, SVGProps } from "react";

export type IconName =
  | "arrow"
  | "check"
  | "chevron"
  | "close"
  | "cloud"
  | "document"
  | "download"
  | "evidence"
  | "external"
  | "eye"
  | "grid"
  | "home"
  | "info"
  | "layers"
  | "lock"
  | "map"
  | "pause"
  | "play"
  | "route"
  | "search"
  | "shield"
  | "spark"
  | "upload"
  | "warning";

const paths: Record<IconName, ReactNode> = {
  arrow: <><path d="M5 12h14"/><path d="m14 7 5 5-5 5"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  chevron: <path d="m9 18 6-6-6-6"/>,
  close: <><path d="m6 6 12 12"/><path d="M18 6 6 18"/></>,
  cloud: <path d="M7 18h10a4 4 0 0 0 .4-8 6 6 0 0 0-11.6-1.6A4.8 4.8 0 0 0 7 18Z"/>,
  document: <><path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></>,
  download: <><path d="M12 3v12"/><path d="m7 10 5 5 5-5M5 21h14"/></>,
  evidence: <><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4M8 11h6M11 8v6"/></>,
  external: <><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v7H4V6h7"/></>,
  eye: <><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="2.5"/></>,
  grid: <><path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"/></>,
  home: <><path d="m3 11 9-8 9 8"/><path d="M5 10v11h14V10M9 21v-7h6v7"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 12 9 5 9-5M3 16l9 5 9-5"/></>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></>,
  map: <><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3Z"/><path d="M9 3v15M15 6v15"/></>,
  pause: <><path d="M8 5v14M16 5v14"/></>,
  play: <path d="m8 5 11 7-11 7Z"/>,
  route: <><circle cx="6" cy="18" r="2"/><circle cx="18" cy="6" r="2"/><path d="M8 18h3a3 3 0 0 0 3-3v-6a3 3 0 0 1 3-3h-1"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4 4"/></>,
  shield: <><path d="M12 3 5 6v5c0 5 3 8 7 10 4-2 7-5 7-10V6Z"/><path d="m9 12 2 2 4-5"/></>,
  spark: <><path d="m12 2 1.5 5.5L19 9l-5.5 1.5L12 16l-1.5-5.5L5 9l5.5-1.5Z"/><path d="m19 15 .7 2.3L22 18l-2.3.7L19 21l-.7-2.3L16 18l2.3-.7Z"/></>,
  upload: <><path d="M12 21V9"/><path d="m7 14 5-5 5 5M5 3h14"/></>,
  warning: <><path d="M12 3 2.5 20h19Z"/><path d="M12 9v5M12 17h.01"/></>,
};

export function Icon({ name, ...props }: { name: IconName } & SVGProps<SVGSVGElement>) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...props}>
      {paths[name]}
    </svg>
  );
}

export function CorridorMark({ compact = false }: { compact?: boolean }) {
  return (
    <span className={`corridor-mark${compact ? " compact" : ""}`} aria-hidden="true">
      <i /><i /><i /><b />
    </span>
  );
}
