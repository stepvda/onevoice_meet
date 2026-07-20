import { useEffect, useState } from "react";

/**
 * Phone / small-touch-device detection, driven by media queries so it stays
 * correct across orientation changes and window resizes (same matchMedia +
 * `change`-listener idiom as `themePref.ts`).
 *
 * `isMobile` is deliberately NOT width-only: a phone held in landscape is
 * usually wider than 640px, so a pure `max-width` test would misclassify it
 * as desktop and route it back to LiveKit's paginating <GridLayout> — the
 * exact component whose area-based pagination hides tiles on small screens.
 * We therefore treat a device as mobile when EITHER:
 *   - the primary pointer is coarse AND cannot hover (a touch phone/tablet,
 *     true in both orientations), OR
 *   - the viewport is small on either axis (narrow phone portrait, or the
 *     short viewport a phone has in landscape; also catches a tiny desktop
 *     window, where the compact grid is harmless and arguably nicer).
 *
 * Because the coarse-pointer signal holds across rotation, a real phone stays
 * on the mobile renderer in both orientations — only the column/row math
 * recomputes, so rotating never remounts the tiles (no black flash / re-subscribe).
 */
function read() {
  const touchPrimary =
    window.matchMedia("(pointer: coarse)").matches &&
    window.matchMedia("(hover: none)").matches;
  const smallViewport =
    window.matchMedia("(max-width: 640px)").matches ||
    window.matchMedia("(max-height: 640px)").matches;
  return {
    isMobile: touchPrimary || smallViewport,
    isPortrait: window.matchMedia("(orientation: portrait)").matches,
  };
}

export function useIsMobile() {
  const [state, setState] = useState(read);
  useEffect(() => {
    const queries = [
      "(pointer: coarse)",
      "(hover: none)",
      "(max-width: 640px)",
      "(max-height: 640px)",
      "(orientation: portrait)",
    ].map((q) => window.matchMedia(q));
    const apply = () => setState(read());
    queries.forEach((mql) => mql.addEventListener("change", apply));
    // Re-read once on mount in case anything changed between the lazy initial
    // read and the listeners attaching.
    apply();
    return () =>
      queries.forEach((mql) => mql.removeEventListener("change", apply));
  }, []);
  return state;
}
