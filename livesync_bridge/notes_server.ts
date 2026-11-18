// Notes web UI + small API straight on the Self-hosted LiveSync CouchDB database.
// Nothing is written to disk: every note is read, decrypted and saved on request.
//
// Ingress (Home Assistant sidebar) talks to it from 172.30.32.2; Home Assistant Core may call
// POST /api/append (voice notes) from 172.30.32.1. Everything else is refused.
import { DirectFileManipulator } from "@vrtmrz/livesync-commonlib";
import { DEVICE_ID_PREFERRED, MILESTONE_DOCID } from "@vrtmrz/livesync-commonlib/compat/common/types";
import { decodeBinary } from "@vrtmrz/livesync-commonlib/compat/string_and_binary/convert";
import { isPlainText } from "@vrtmrz/livesync-commonlib/compat/string_and_binary/path";
import { createBinaryBlob, createTextBlob } from "@vrtmrz/livesync-commonlib/compat/common/utils";
import { defaultLoggerEnv, LOG_LEVEL_NOTICE } from "octagonal-wheels/common/logger";

defaultLoggerEnv.minLogLevel = LOG_LEVEL_NOTICE;

const PORT = 8099;
const INGRESS_IP = "172.30.32.2";
const CORE_IP = "172.30.32.1";
const UI = await Deno.readTextFile(new URL("./notes_ui/index.html", import.meta.url));

const o = JSON.parse(await Deno.readTextFile("/data/options.json"));
const tz = o.timezone || "Europe/Kyiv";
const noteFolder = (o.voice_note_folder || "Claude Note").replace(/^\/+|\/+$/g, "");

// deno-lint-ignore no-explicit-any
const conf: Record<string, any> = {
  url: o.couchdb_url.replace(/\/+$/, ""),
  database: o.database,
  username: o.username,
  password: o.password,
  passphrase: o.passphrase || "",
  obfuscatePassphrase: o.path_obfuscation ? o.passphrase : "",
  pathNormalizationVersion: o.unicode_nfc_paths ? 1 : 0,
  handleFilenameCaseSensitive: !!o.filename_case_sensitive,
};

// Take chunking / encryption settings from the vault itself, like the bridge's useRemoteTweaks.
async function applyRemoteTweaks() {
  const auth = "Basic " + btoa(unescape(encodeURIComponent(`${conf.username}:${conf.password}`)));
  const res = await fetch(`${conf.url}/${conf.database}/${encodeURIComponent(MILESTONE_DOCID)}`, {
    headers: { Authorization: auth },
  });
  if (!res.ok) throw new Error(`CouchDB answered ${res.status} for the vault settings document`);
  const w = await res.json();
  const t = w?.tweak_values?.[DEVICE_ID_PREFERRED] ?? Object.values(w?.tweak_values ?? {})[0];
  if (!t) return;
  if (t.encrypt && !conf.passphrase) throw new Error("The vault is encrypted: set the passphrase option.");
  if (t.usePathObfuscation && !conf.obfuscatePassphrase) {
    throw new Error("The vault uses path obfuscation: turn path_obfuscation on (needs the passphrase).");
  }
  for (const k of [
    "customChunkSize", "minimumChunkSize", "hashAlg", "maxAgeInEden", "maxTotalLengthInEden", "maxChunksInEden",
    "useEden", "enableCompression", "useDynamicIterationCount", "enableChunkSplitterV2", "chunkSplitterVersion",
    "E2EEAlgorithm", "doNotUseFixedRevisionForChunks", "idDerivationVersion", "encryptInternalMetadata",
  ]) if (t[k] !== undefined) conf[k] = t[k];
  conf.handleFilenameCaseSensitive = t.handleFilenameCaseSensitive ?? conf.handleFilenameCaseSensitive;
}

let man: DirectFileManipulator | undefined;
let lastError = "";
async function connect() {
  for (let attempt = 1; ; attempt++) {
    try {
      await applyRemoteTweaks();
      const m = new DirectFileManipulator(conf as never, { fetch: (r, i) => globalThis.fetch(r, i) });
      await m.ready.promise;
      man = m;
      lastError = "";
      console.log(`Connected to ${conf.url}/${conf.database}${conf.passphrase ? " (end-to-end encrypted)" : ""}`);
      return;
    } catch (e) {
      lastError = e instanceof Error ? e.message : String(e);
      const delay = Math.min(60, 2 ** Math.min(attempt, 6));
      console.error(`Could not open the vault (attempt ${attempt}): ${lastError}; retrying in ${delay}s`);
      await new Promise((r) => setTimeout(r, delay * 1000));
    }
  }
}
connect();

// ---- vault access ----------------------------------------------------------------------------
type Item = { path: string; mtime: number; size: number };
let listCache: { at: number; items: Item[] } | undefined;

function okPath(p: string | null): p is string {
  return !!p && !p.startsWith("/") && !p.includes(":") && !p.startsWith(".") &&
    p.split("/").every((s) => s !== "" && s !== "." && s !== "..");
}

async function list(force = false): Promise<Item[]> {
  if (!force && listCache && Date.now() - listCache.at < 15000) return listCache.items;
  const items: Item[] = [];
  // deno-lint-ignore no-explicit-any
  for await (const d of man!.enumerateAllNormalDocs({ metaOnly: true }) as AsyncGenerator<any>) {
    if (d.deleted || d._deleted || !d.path || String(d.path).startsWith("i:")) continue;
    items.push({ path: d.path, mtime: d.mtime, size: d.size });
  }
  items.sort((a, b) => a.path.localeCompare(b.path));
  listCache = { at: Date.now(), items };
  return items;
}

async function read(path: string) {
  // deno-lint-ignore no-explicit-any
  const d = await man!.get(path as never) as any;
  if (!d || d.deleted) return undefined;
  return d.type === "newnote"
    ? { binary: new Uint8Array(decodeBinary(d.data)), mtime: d.mtime, ctime: d.ctime }
    : { text: (d.data as string[]).join(""), mtime: d.mtime, ctime: d.ctime };
}

async function writeText(path: string, text: string, ctime?: number) {
  const blob = createTextBlob([text]);
  const now = Date.now();
  const ok = await man!.put(path, blob, { ctime: ctime ?? now, mtime: now, size: blob.size },
    isPlainText(path) ? "plain" : "newnote");
  listCache = undefined;
  return ok;
}

// Voice notes are appended one after another; a queue keeps two of them from overwriting each other.
let appendQueue: Promise<unknown> = Promise.resolve();
function appendNote(text: string) {
  const job = appendQueue.then(async () => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
      timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date()).map((p) => [p.type, p.value]));
    const path = `${noteFolder}/${parts.year}-${parts.month}-${parts.day}.md`;
    const old = await read(path);
    const head = old?.text ?? `# Голосовые заметки ${parts.day}.${parts.month}.${parts.year}\n\n`;
    const body = head + (head.endsWith("\n") ? "" : "\n") + `- ${parts.hour}:${parts.minute} ${text}\n`;
    if (!await writeText(path, body, old?.ctime)) throw new Error("the vault refused the write");
    return path;
  });
  appendQueue = job.catch(() => {});
  return job;
}

// ---- HTTP --------------------------------------------------------------------------------------
const MIME: Record<string, string> = {
  png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", svg: "image/svg+xml",
  pdf: "application/pdf", mp3: "audio/mpeg", m4a: "audio/mp4", mp4: "video/mp4", webm: "video/webm",
};
const json = (v: unknown, status = 200) =>
  new Response(JSON.stringify(v), { status, headers: { "content-type": "application/json; charset=utf-8" } });

Deno.serve({ port: PORT, hostname: "0.0.0.0" }, async (req, info) => {
  const ip = (info.remoteAddr as Deno.NetAddr).hostname;
  const url = new URL(req.url);
  const route = url.pathname.replace(/\/+$/, "") || "/";
  const fromIngress = ip === INGRESS_IP;
  const fromCore = ip === CORE_IP && route === "/api/append" && req.method === "POST";
  if (!fromIngress && !fromCore) return new Response("Forbidden", { status: 403 });

  if (route === "/") return new Response(UI, { headers: { "content-type": "text/html; charset=utf-8" } });
  if (!man) return json({ error: lastError ? `Нет доступа к базе: ${lastError}` : "Подключаюсь к базе…" }, 503);

  try {
    if (route === "/api/list") return json(await list(url.searchParams.has("refresh")));

    if (route === "/api/append" && req.method === "POST") {
      const b = await req.json().catch(() => ({}));
      const text = String(b.text ?? "").replace(/\s+/g, " ").trim().slice(0, 2000);
      if (!text) return json({ error: "empty text" }, 400);
      return json({ path: await appendNote(text) });
    }

    if (route === "/api/file") {
      const path = url.searchParams.get("path");
      if (!okPath(path)) return json({ error: "bad path" }, 400);
      if (req.method === "GET") {
        const f = await read(path);
        if (!f) return json({ error: "not found" }, 404);
        if (f.text !== undefined) return json({ text: f.text, mtime: f.mtime });
        const ext = path.split(".").pop()!.toLowerCase();
        return new Response(f.binary, {
          headers: { "content-type": MIME[ext] ?? "application/octet-stream", "cache-control": "private, max-age=300" },
        });
      }
      if (req.method === "PUT") {
        if (!isPlainText(path)) return json({ error: "only text notes can be edited here" }, 400);
        const b = await req.json();
        const old = await read(path);
        if (old && b.baseMtime && old.mtime > b.baseMtime) {
          return json({ error: "Заметку изменили на другом устройстве, пока ты её редактировал. Обнови и повтори." }, 409);
        }
        if (!await writeText(path, String(b.text ?? ""), old?.ctime)) return json({ error: "write refused" }, 500);
        const now = await read(path);
        return json({ ok: true, mtime: now?.mtime });
      }
      if (req.method === "DELETE") {
        const ok = await man.delete(path);
        listCache = undefined;
        return json({ ok }, ok ? 200 : 500);
      }
    }
    return json({ error: "not found" }, 404);
  } catch (e) {
    console.error(e);
    return json({ error: e instanceof Error ? e.message : String(e) }, 500);
  }
});
console.log(`Notes UI listening on :${PORT}`);
