import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/atkinson-hyperlegible/latin-400.css";
import "@fontsource/atkinson-hyperlegible/latin-700.css";
import "@fontsource/barlow-condensed/latin-500.css";
import "@fontsource/barlow-condensed/latin-600.css";
import "@fontsource/barlow-condensed/latin-700.css";
import "@fontsource/ibm-plex-mono/latin-400.css";
import "@fontsource/ibm-plex-mono/latin-500.css";
import { PublicReplayApp } from "./PublicReplayApp";
import { loadPublicRidgewayFixture } from "./replayData";
import "./replay.css";

const root = document.getElementById("root");
if (!root) throw new Error("Public replay root is unavailable.");

let replay: ReactNode;
try {
  replay = <PublicReplayApp fixture={loadPublicRidgewayFixture()} />;
} catch {
  replay = <main className="replay-load-error">
    <p>RIDGEWAY CROSSING · PUBLIC REPLAY</p>
    <h1>Direct evidence is unavailable</h1>
    <p>The synthetic projection did not pass its local integrity checks. No portfolio identity, cost, rank, or connectivity value has been shown.</p>
  </main>;
}

createRoot(root).render(<StrictMode>{replay}</StrictMode>);
