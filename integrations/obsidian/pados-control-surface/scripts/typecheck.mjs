import { readFileSync } from "node:fs";

const source = readFileSync("src/main.ts", "utf8");
for (const required of ["type PadosSettings", "assertLocalhost", "PadosControlSurfacePlugin"]) {
  if (!source.includes(required)) {
    console.error(`missing TypeScript contract: ${required}`);
    process.exit(1);
  }
}
console.log("plugin typecheck contract passed");
