import React from "react";
import { describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import Sidebar from "../src/components/Sidebar.jsx";
import { setViewport } from "./setup.js";

const health = {
  trial_count: 1234,
  degraded_mode: false,
  data_current_through: "2026-07-01",
  normalization_version: "1.1.0-disease-purpose-gates",
};

function renderSidebar(width) {
  setViewport(width);
  return render(<Sidebar user="dr.smith" health={health} onLogout={() => {}} />);
}

describe("navigation drawer (mobile)", () => {
  it("keeps every nav destination in the DOM at 375px", () => {
    renderSidebar(375);
    const nav = screen.getByRole("navigation", { name: /workflow steps/i });
    ["Intake", "De-ID review", "Patient profile", "Trial board", "Handoff"].forEach((label) => {
      expect(within(nav).getByText(new RegExp(label, "i"))).toBeInTheDocument();
    });
  });

  it("exposes a hamburger toggle that reports its state", () => {
    renderSidebar(375);
    const toggle = screen.getByRole("button", { name: /open navigation menu/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "workspace-nav");

    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: /close navigation menu/i })).toHaveAttribute(
      "aria-expanded",
      "true"
    );
  });

  it("opens with a backdrop and moves focus into the panel", () => {
    const { container } = renderSidebar(375);
    fireEvent.click(screen.getByRole("button", { name: /open navigation menu/i }));

    const panel = container.querySelector("#workspace-nav");
    expect(panel.className).toContain("open");
    expect(screen.getByTestId("drawer-backdrop")).toBeInTheDocument();
    expect(panel.contains(document.activeElement)).toBe(true);
  });

  it("closes on Escape and returns focus to the toggle", () => {
    const { container } = renderSidebar(375);
    const toggle = screen.getByRole("button", { name: /open navigation menu/i });
    fireEvent.click(toggle);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(container.querySelector("#workspace-nav").className).not.toContain("open");
    expect(screen.queryByTestId("drawer-backdrop")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /open navigation menu/i }));
  });

  it("closes when the backdrop is clicked", () => {
    const { container } = renderSidebar(375);
    fireEvent.click(screen.getByRole("button", { name: /open navigation menu/i }));
    fireEvent.click(screen.getByTestId("drawer-backdrop"));
    expect(container.querySelector("#workspace-nav").className).not.toContain("open");
  });

  it("closes after following a nav link", () => {
    const { container } = renderSidebar(375);
    fireEvent.click(screen.getByRole("button", { name: /open navigation menu/i }));
    fireEvent.click(screen.getByText(/4 · Trial board/i));
    expect(container.querySelector("#workspace-nav").className).not.toContain("open");
  });

  it("traps Tab inside the open panel", () => {
    const { container } = renderSidebar(375);
    fireEvent.click(screen.getByRole("button", { name: /open navigation menu/i }));
    const panel = container.querySelector("#workspace-nav");

    const focusables = panel.querySelectorAll('a[href], button:not([disabled])');
    const last = focusables[focusables.length - 1];
    last.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(panel.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(focusables[0]);

    // Shift+Tab from the first wraps back to the last.
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
  });

  it("resets when the viewport widens back to desktop", () => {
    const { container } = renderSidebar(375);
    fireEvent.click(screen.getByRole("button", { name: /open navigation menu/i }));
    expect(container.querySelector("#workspace-nav").className).toContain("open");

    act(() => setViewport(1280));
    expect(container.querySelector("#workspace-nav").className).not.toContain("open");
    expect(screen.queryByTestId("drawer-backdrop")).toBeNull();
  });
});

describe("navigation rail (desktop)", () => {
  it("renders the nav without drawer chrome at 1280px", () => {
    const { container } = renderSidebar(1280);
    const panel = container.querySelector("#workspace-nav");
    expect(panel.className).not.toContain("is-drawer");
    expect(screen.queryByTestId("drawer-backdrop")).toBeNull();
    expect(screen.getByRole("navigation", { name: /workflow steps/i })).toBeInTheDocument();
  });
});

describe("sidebar rail", () => {
  it("does not crash before /health resolves", () => {
    setViewport(1280);
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<Sidebar user="dr.smith" health={null} onLogout={() => {}} />);
    expect(screen.getByRole("navigation", { name: /workflow steps/i })).toBeInTheDocument();
    spy.mockRestore();
  });
});
