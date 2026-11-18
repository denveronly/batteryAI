// Builds the livesync-bridge config (/data/config.json) from the add-on options.
const o = JSON.parse(await Deno.readTextFile("/data/options.json"));

const trim = (s: string) => (s || "").trim().replace(/^\/+|\/+$/g, "");
const folder = "/share/" + trim(o.share_folder);
const sub = trim(o.vault_subfolder);

if (!o.password) {
  console.error("Set the CouchDB password in the add-on configuration.");
  Deno.exit(1);
}
if (o.path_obfuscation && !o.passphrase) {
  console.error("Path obfuscation needs the end-to-end encryption passphrase.");
  Deno.exit(1);
}
await Deno.mkdir(folder, { recursive: true });

const common = {
  group: "vault",
  pathNormalizationVersion: o.unicode_nfc_paths ? 1 : 0,
  handleFilenameCaseSensitive: !!o.filename_case_sensitive,
};
const config = {
  peers: [
    {
      ...common,
      type: "couchdb",
      name: "couchdb",
      url: o.couchdb_url.replace(/\/+$/, ""),
      database: o.database,
      username: o.username,
      password: o.password,
      passphrase: o.passphrase || "",
      obfuscatePassphrase: o.path_obfuscation ? o.passphrase : "",
      baseDir: sub ? sub + "/" : "",
      useRemoteTweaks: true,
    },
    {
      ...common,
      type: "storage",
      name: "share",
      baseDir: folder + "/",
      scanOfflineChanges: true,
      ignore: [".trash/**", "**/.DS_Store"],
    },
  ],
};
await Deno.writeTextFile("/data/config.json", JSON.stringify(config, null, 2));
console.log(`Vault ${sub ? "folder \"" + sub + "\"" : "(whole)"} <-> ${folder} via ${config.peers[0].url}/${o.database}` +
  `${o.passphrase ? " (end-to-end encrypted)" : ""}`);
