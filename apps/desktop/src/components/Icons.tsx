import type { SVGProps } from "react";

type IconName =
  | "arrow"
  | "check"
  | "chevron"
  | "close"
  | "download"
  | "file"
  | "folder"
  | "info"
  | "layers"
  | "map"
  | "pause"
  | "play"
  | "plus"
  | "rain"
  | "search"
  | "shield"
  | "spark"
  | "trace"
  | "upload"
  | "warning";

const paths: Record<IconName, React.ReactNode> = {
  arrow: <><path d="M5 12h14"/><path d="m13 6 6 6-6 6"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  chevron: <path d="m9 18 6-6-6-6"/>,
  close: <><path d="m6 6 12 12"/><path d="m18 6-12 12"/></>,
  download: <><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></>,
  file: <><path d="M6 2h8l4 4v16H6z"/><path d="M14 2v5h4"/></>,
  folder: <path d="M3 6h7l2 2h9v11H3z"/>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6"/><path d="M12 7h.01"/></>,
  layers: <><path d="m12 3 9 5-9 5-9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 16 9 5 9-5"/></>,
  map: <><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15"/><path d="M15 6v15"/></>,
  pause: <><path d="M8 5v14"/><path d="M16 5v14"/></>,
  play: <path d="m8 5 11 7-11 7z"/>,
  plus: <><path d="M12 5v14"/><path d="M5 12h14"/></>,
  rain: <><path d="M7 14a4 4 0 0 1 .6-7.95A5.5 5.5 0 0 1 18 8a3 3 0 0 1-1 5.83"/><path d="m8 17-1 2"/><path d="m13 16-1 2"/><path d="m18 17-1 2"/></>,
  search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/></>,
  shield: <><path d="M12 3 4 6v5c0 5 3.5 8.4 8 10 4.5-1.6 8-5 8-10V6z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></>,
  spark: <><path d="m12 3 1.4 4.6L18 9l-4.6 1.4L12 15l-1.4-4.6L6 9l4.6-1.4z"/><path d="m5 15 .8 2.2L8 18l-2.2.8L5 21l-.8-2.2L2 18l2.2-.8z"/></>,
  trace: <><circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 5h5a4 4 0 0 1 4 4v6a4 4 0 0 0 3 4"/></>,
  upload: <><path d="M12 21V7"/><path d="m7 12 5-5 5 5"/><path d="M5 3h14"/></>,
  warning: <><path d="M12 3 2.5 20h19z"/><path d="M12 9v5"/><path d="M12 17h.01"/></>,
};

export function Icon({ name, ...props }: SVGProps<SVGSVGElement> & { name: IconName }) {
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" {...props}>
      {paths[name]}
    </svg>
  );
}
