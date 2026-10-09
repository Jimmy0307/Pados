import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

mkdirSync("dist", { recursive: true });
const source = readFileSync("src/main.ts", "utf8")
  .replaceAll("export class", "class")
  .replaceAll("export default PadosControlSurfacePlugin;", "module.exports = PadosControlSurfacePlugin;");
writeFileSync("dist/main.js", source, "utf8");
copyFileSync("manifest.json", "dist/manifest.json");
mkdirSync(dirname("dist/styles.css"), { recursive: true });
writeFileSync("dist/styles.css", ".pados-control-surface{display:block;}\n", "utf8");
console.log("plugin build passed");
