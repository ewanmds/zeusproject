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

for (const file of ["index.html", "index.css", "app.js", "favicon.svg"]) {
    await cp(new URL(file, source), new URL(file, output));
}

// Content hashes keep every asset cache-busted without a separate module.
const hash = async (file) => createHash("sha256")
    .update(await readFile(new URL(file, source))).digest("hex").slice(0, 12);
const version = await hash("app.js");
const cssVersion = await hash("index.css");

const config = {
    // Same origin by default: a local or self-hosted build must not send the
    // operator's access code to a third-party host. Set ZEUS_API_BASE only for
    // an intentional split deployment.
    apiBase: process.env.ZEUS_API_BASE || "",
    apiToken: process.env.ZEUS_API_TOKEN || "",
    wsUrl: process.env.ZEUS_WS_URL || "",
};
const configText = `window.ZEUS_CONFIG = ${JSON.stringify(config)};\n`;
await writeFile(new URL("config.js", output), configText, "utf8");
const configVersion = createHash("sha256").update(configText).digest("hex").slice(0, 12);

const html = await readFile(new URL("index.html", output), "utf8");
await writeFile(new URL("index.html", output), html
    .replace(/(app\.js)\?v=[^"\s]+/g, `$1?v=${version}`)
    .replace(/(index\.css)(\?v=[^"\s]+)?/g, `$1?v=${cssVersion}`)
    .replace(/(config\.js)(\?v=[^"\s]+)?/g, `$1?v=${configVersion}`), "utf8");

await cp(new URL("assets", source), new URL("assets", output), { recursive: true });
