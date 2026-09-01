import { readFile, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const [modulePath, inputPath, outputPath] = process.argv.slice(2);
if (!modulePath || !inputPath || !outputPath) throw new Error("usage: render.mjs MODULE INPUT OUTPUT");
const { instance } = await import(pathToFileURL(modulePath));
const viz = await instance();
if (viz.graphvizVersion !== "15.1.1") throw new Error(`Graphviz runtime version mismatch: ${viz.graphvizVersion}`);
const dot = await readFile(inputPath, "utf8");
let svg = viz.renderString(dot, { engine: "dot", format: "svg" });
const start = svg.indexOf("<svg ");
if (start < 0) throw new Error("Graphviz did not return SVG");
svg = svg.slice(start).replace(/<!--(?:.|\n)*?-->/g, "").replace(/\n{2,}/g, "\n");
await writeFile(outputPath, svg.endsWith("\n") ? svg : `${svg}\n`, "utf8");
