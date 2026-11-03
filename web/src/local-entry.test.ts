// SPDX-License-Identifier: AGPL-3.0-only
// Usability walkthrough: a local user who opens the web root lands on the Remote sign-in and needs a way to the local
// console; an empty project list must say how to create a project instead of "Loading projects…" forever.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { projectPlaceholder } from "./project-placeholder";
import { LocalConsoleHint } from "./remote-console";

describe("local entry points", () => {
  it("offers the local operator console from the Remote sign-in", () => {
    const html = renderToStaticMarkup(createElement(LocalConsoleHint));
    expect(html).toContain('href="/operator.html"');
    expect(html).toMatch(/this computer/);
    expect(html).toMatch(/Remote Operations/);
  });

  it("tells an empty, a failed and a loading project list apart", () => {
    expect(projectPlaceholder({ loaded: false, failed: false, count: 0 })).toBe("Loading projects…");
    expect(projectPlaceholder({ loaded: true, failed: false, count: 0 })).toMatch(/pados project create/);
    expect(projectPlaceholder({ loaded: true, failed: true, count: 0 })).toMatch(/could not be loaded/);
    expect(projectPlaceholder({ loaded: true, failed: false, count: 2 })).toBeNull();
  });
});
