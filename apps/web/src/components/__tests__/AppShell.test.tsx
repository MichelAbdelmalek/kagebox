import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AppShell } from "../AppShell";

describe("AppShell", () => {
  it("marks the current page and labels the main navigation", () => {
    render(
      <AppShell current="/projects" profile="cloud">
        <h1>Projects</h1>
      </AppShell>,
    );
    expect(screen.getByRole("navigation", { name: "Main" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Projects" }).getAttribute("aria-current")).toBe("page");
  });

  it("does not render unbuilt screens as links", () => {
    render(
      <AppShell current="/projects" profile="cloud">
        <p>x</p>
      </AppShell>,
    );
    expect(screen.queryByRole("link", { name: /Voice casting/ })).toBeNull();
    expect(screen.getAllByText("Phase 1").length).toBeGreaterThan(0);
  });
});
