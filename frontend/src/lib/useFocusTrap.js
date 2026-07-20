import { useEffect } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

/**
 * Trap Tab focus inside `containerRef` while `active`, close on Escape, and
 * restore focus to the opener when the trap releases.
 *
 * A slide-out panel that leaves focus behind it is a keyboard trap in the other
 * direction: the user tabs into content they cannot see. The trap and the focus
 * restore are what make the drawer usable without a pointer.
 */
export function useFocusTrap(containerRef, active, { onEscape, returnFocusTo } = {}) {
  useEffect(() => {
    if (!active) return undefined;
    const container = containerRef.current;
    if (!container) return undefined;

    const previouslyFocused = document.activeElement;
    const focusables = () =>
      Array.from(container.querySelectorAll(FOCUSABLE)).filter(
        (el) => el.getAttribute("aria-hidden") !== "true" && el.tabIndex !== -1
      );

    const initial = focusables()[0];
    if (initial) initial.focus();

    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.preventDefault();
        if (onEscape) onEscape();
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusables();
      if (items.length === 0) {
        event.preventDefault();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const current = document.activeElement;

      if (event.shiftKey) {
        if (current === first || !container.contains(current)) {
          event.preventDefault();
          last.focus();
        }
      } else if (current === last || !container.contains(current)) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("keydown", onKeyDown, true);
      const target = (returnFocusTo && returnFocusTo.current) || previouslyFocused;
      if (target && typeof target.focus === "function" && document.contains(target)) {
        target.focus();
      }
    };
  }, [active, containerRef, onEscape, returnFocusTo]);
}
