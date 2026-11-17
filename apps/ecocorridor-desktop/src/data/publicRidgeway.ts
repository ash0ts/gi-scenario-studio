import projection from "./ridgewayFixture.json";
import { parseRidgewayFixture } from "./ridgeway";
export { PUBLIC_RIDGEWAY_EVIDENCE_MANIFEST_SHA256 } from "./trustedManifests";

// This static, synthetic projection is used only by the no-login public replay.
// The desktop guided workflow obtains its projection from connectivity.guided.open.
export const publicRidgewayFixture = parseRidgewayFixture(projection);
