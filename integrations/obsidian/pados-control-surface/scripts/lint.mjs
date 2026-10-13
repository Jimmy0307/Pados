import { readFileSync } from "node:fs";

const source = readFileSync("src/main.ts", "utf8");
const forbidden = ["telemetry", "cloud", "localStorage", "password", "process.env"];
const findings = forbidden.filter((term) => source.includes(term));
if (findings.length > 0) {
  console.error(`forbidden plugin terms: ${findings.join(", ")}`);
  process.exit(1);
}
console.log("plugin lint passed");
