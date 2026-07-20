import { useEffect, useState } from "react";

/**
 * Subscribe to a CSS media query. Returns false in environments without
 * matchMedia (SSR, jsdom without a stub) rather than throwing, so the desktop
 * layout is the safe default and the nav is never left unreachable.
 */
export function useMediaQuery(query) {
  const read = () =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false;

  const [matches, setMatches] = useState(read);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return undefined;
    const mql = window.matchMedia(query);
    const onChange = (e) => setMatches(e.matches);
    setMatches(mql.matches);
    if (mql.addEventListener) mql.addEventListener("change", onChange);
    else if (mql.addListener) mql.addListener(onChange);
    return () => {
      if (mql.removeEventListener) mql.removeEventListener("change", onChange);
      else if (mql.removeListener) mql.removeListener(onChange);
    };
  }, [query]);

  return matches;
}
