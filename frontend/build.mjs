import { cp, mkdir, rm, readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const source = new URL("./", import.meta.url);
const output = new URL("./dist/", import.meta.url);

if (resolve(fileURLToPath(output)) !== resolve(dirname(fileURLToPath(import.meta.url)), "dist")) {
    throw new Error("Build output must stay inside frontend/dist");
}
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
await mkdir(new URL("frontend/", output), { recursive: true });

for (const file of ["index.html", "index.css", "state.js", "app.js", "favicon.svg"]) {
    await cp(new URL(file, source), new URL(file, output));
}
// Match the Python server's canonical module URL, including older running servers.
await cp(new URL("state.js", source), new URL("frontend/state.js", output));

// A single content version keeps state helpers and the renderer in sync across
// static deployments and browser caches. No API credentials enter this hash.
const version = createHash("sha256")
    .update(await readFile(new URL("state.js", source)))
    .update(await readFile(new URL("app.js", source))).digest("hex").slice(0, 12);
const html = await readFile(new URL("index.html", output), "utf8");
await writeFile(new URL("index.html", output), html.replace(
    /((?:state|app)\.js)\?v=[^"\s]+/g, `$1?v=${version}`), "utf8");

// Static assets (HIP-3 market icons downloaded from Hyperliquid).
await cp(new URL("assets", source), new URL("assets", output), { recursive: true });

const config = {
    // This URL is public frontend configuration, not a wallet secret.
    apiBase: process.env.ZEUS_API_BASE || "https://3-113-242-142.sslip.io",
    apiToken: process.env.ZEUS_API_TOKEN || "",
    wsUrl: process.env.ZEUS_WS_URL || "",
};

await writeFile(
    new URL("config.js", output),
    `window.ZEUS_CONFIG = ${JSON.stringify(config)};\n`,
    "utf8",
);
