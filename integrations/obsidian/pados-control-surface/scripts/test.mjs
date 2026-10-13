import { readFileSync } from "node:fs";

const manifest = JSON.parse(readFileSync("manifest.json", "utf8"));
if (manifest.isDesktopOnly !== true) {
  throw new Error("plugin must be desktop-only");
}
const source = readFileSync("src/main.ts", "utf8");
if (!source.includes("http://127.0.0.1:8088")) {
  throw new Error("plugin must default to localhost API");
}
if (!source.includes("registerMarkdownCodeBlockProcessor")) {
  throw new Error("markdown pados-control processor missing");
}
console.log("plugin unit tests passed");
