import { cp, mkdir, rm, writeFile } from "node:fs/promises";

const source = new URL("./", import.meta.url);
const output = new URL("./dist/", import.meta.url);

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

for (const file of ["index.html", "index.css", "app.js"]) {
    await cp(new URL(file, source), new URL(file, output));
}

const config = {
    // This URL is public frontend configuration, not a wallet secret.
    apiBase: process.env.ZEUS_API_BASE || "",
    apiToken: process.env.ZEUS_API_TOKEN || "",
    wsUrl: process.env.ZEUS_WS_URL || "",
};

await writeFile(
    new URL("config.js", output),
    `window.ZEUS_CONFIG = ${JSON.stringify(config)};\n`,
    "utf8",
);
