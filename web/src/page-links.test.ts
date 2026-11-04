// SPDX-License-Identifier: AGPL-3.0-only
// Review findings: the inbox's "Open" for a result acceptance pointed at an API route (/remote/v1/approvals), not a
// page where a person can decide; and page links were absolute ("/operator.html"), which leave a sub-path build
// (base "/Pados/").
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { inboxAction, pageHref, remoteInitialView } from "./page-links";
import { ActionsSignInHint, LocalConsoleHint } from "./remote-console";

describe("page links under any base", () => {
  it.each([
    ["/", "/", "/operator.html"],
    ["/Pados/", "/Pados/", "/Pados/operator.html"],
    ["/Pados", "/Pados/", "/Pados/operator.html"],
    ["./", "./", "./operator.html"],
  ])("base %s", (base, remote, operator) => {
    expect(pageHref("remote", base)).toBe(remote);
    expect(pageHref("operator", base)).toBe(operator);
    expect(pageHref("remote", base, { view: "actions" })).toBe(`${remote}?view=actions`);
  });

  it("links the local console under the build's base", () => {
    expect(renderToStaticMarkup(createElement(LocalConsoleHint, { base: "/Pados/" }))).toContain('href="/Pados/operator.html"');
    expect(renderToStaticMarkup(createElement(LocalConsoleHint, { base: "/" }))).toContain('href="/operator.html"');
  });

  it("has no absolute page links left in the console sources", () => {
    const root = fileURLToPath(new URL(".", import.meta.url));
    const sources = readdirSync(root).filter((name) => name.endsWith(".tsx"));
    for (const name of sources) {
      const text = readFileSync(join(root, name), "utf8");
      expect(text, name).not.toMatch(/href="\/[^"]*\.html/);
      expect(text, name).not.toMatch(/href=\{?["'`]\/(operator|remote|index)\.html/);
    }
  });
});

describe("the Remote console deep link", () => {
  it("opens the requested view, and only a known one", () => {
    expect(remoteInitialView("?view=actions")).toBe("actions");
    expect(remoteInitialView("?view=tasks&x=1")).toBe("tasks");
    expect(remoteInitialView("?view=<script>")).toBe("map");
    expect(remoteInitialView("")).toBe("map");
  });

  it("tells a person what they need before they can decide", () => {
    const html = renderToStaticMarkup(createElement(ActionsSignInHint));
    expect(html).toMatch(/Actions view opens/);
    expect(html).toMatch(/PADOS_REMOTE_CONFIG/);
    expect(html).toMatch(/WebAuthn device/);
    expect(html).toMatch(/Register this device/);
  });
});

describe("where an inbox item is acted on", () => {
  it("sends a result acceptance to the Remote console's Actions view with its prerequisites", () => {
    const action = inboxAction({ surface: "REMOTE_CONSOLE", view: "actions", approval_id: "ap9",
      needs: ["REMOTE_SIGN_IN", "ENROLLED_WEBAUTHN_DEVICE"] }, "/remote/v1/approvals", "/Pados/");
    expect(action).toMatchObject({ kind: "link", href: "/Pados/?view=actions" });
    expect(action.kind === "link" && action.hint).toMatch(/sign in .*WebAuthn device enrolled/);
    expect(JSON.stringify(action)).not.toContain("/remote/v1/approvals");
  });

  it("reviews a work package approval in the Control Center", () => {
    expect(inboxAction({ surface: "CONTROL_CENTER", view: "approvals", approval_id: "ap8", needs: ["CONTROL_TOKEN"] },
      "/control/approvals/ap8", "/")).toMatchObject({ kind: "in-console", approvalId: "ap8" });
  });

  it("shows an API-only item as a reference, never as a broken link", () => {
    expect(inboxAction({ surface: "API", path: "/control/projects/p1" }, "/control/projects/p1", "/"))
      .toMatchObject({ kind: "reference", path: "/control/projects/p1" });
    expect(inboxAction(undefined, "/product-loop/runs/r1", "/")).toMatchObject({ kind: "reference" });
  });
});
