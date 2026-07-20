import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom has no matchMedia. Tests that need a specific viewport call setViewport()
// below; without it the desktop layout is assumed.
let currentWidth = 1280;
const listeners = new Set();

function evaluate(query) {
  // Supports the only forms this app uses: (max-width: Npx) / (min-width: Npx).
  const max = /\(max-width:\s*(\d+)px\)/.exec(query);
  if (max) return currentWidth <= Number(max[1]);
  const min = /\(min-width:\s*(\d+)px\)/.exec(query);
  if (min) return currentWidth >= Number(min[1]);
  return false;
}

export function setViewport(width) {
  currentWidth = width;
  window.innerWidth = width;
  listeners.forEach((entry) => {
    const matches = evaluate(entry.query);
    if (matches !== entry.matches) {
      entry.matches = matches;
      entry.handlers.forEach((h) => h({ matches, media: entry.query }));
    }
  });
}

window.matchMedia = (query) => {
  const entry = { query, matches: evaluate(query), handlers: new Set() };
  listeners.add(entry);
  return {
    get matches() {
      entry.matches = evaluate(query);
      return entry.matches;
    },
    media: query,
    addEventListener: (_type, handler) => entry.handlers.add(handler),
    removeEventListener: (_type, handler) => entry.handlers.delete(handler),
    addListener: (handler) => entry.handlers.add(handler),
    removeListener: (handler) => entry.handlers.delete(handler),
    dispatchEvent: () => false,
    onchange: null,
  };
};

afterEach(() => {
  cleanup();
  listeners.clear();
  currentWidth = 1280;
  vi.restoreAllMocks();
});
