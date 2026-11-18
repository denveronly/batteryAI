# Obsidian LiveSync Bridge

Opens the notes of an Obsidian **Self-hosted LiveSync** vault in the Home Assistant sidebar
(**Obsidian** panel, administrators only). Notes are read, decrypted and saved straight in the
CouchDB database on every request. **Nothing is copied to disk**, so the vault does not end up in
`/share`, in other add-ons or in Home Assistant backups.

Home Assistant can also add lines to a daily note (voice notes) through a small internal API.

Built on [livesync-bridge](https://github.com/vrtmrz/livesync-bridge) and its library by vrtmrz.

## Configuration

| Option | What it is |
|---|---|
| `couchdb_url` | CouchDB address, the same one the LiveSync plugin uses, e.g. `http://192.168.1.2:5984` |
| `database` | Database name from the plugin settings |
| `username`, `password` | CouchDB user from the plugin settings |
| `passphrase` | End-to-end encryption passphrase, if *End-to-End Encryption* is on in the plugin |
| `path_obfuscation` | Turn on if *Path Obfuscation* is on in the plugin (needs the passphrase) |
| `voice_note_folder` | Vault folder for daily voice notes, default `Claude Note` |
| `timezone` | Time zone for the voice-note dates and times |
| `unicode_nfc_paths` | Must match the vault's path normalisation. Leave off unless you know the vault uses NFC |
| `filename_case_sensitive` | Must match *Handle files as Case-Sensitive* in the plugin |
| `mirror_to_share` | Off by default. On: also keep a full two-way **plain-text copy** of the vault in `/share/<share_folder>` (the original livesync-bridge) |
| `share_folder`, `vault_subfolder`, `reset_on_start` | Only for the mirror |

Chunk size, encryption and other settings are taken from the vault itself.

The vault must be on database format 12 or newer: update the LiveSync plugin on your devices and
sync once if the log says the database version is not compatible.

## Panel

- Folder tree and search by name on the left; notes are shown rendered, with `[[wikilinks]]`
  and embedded images.
- **Править** edits the Markdown, **Сохранить** (or Ctrl+S) writes it to the vault. If the note
  was changed on another device in the meantime, saving is refused instead of overwriting it.
- **Новая** creates a note, 🗑 deletes it from the vault (on every device).

## Voice notes API

Home Assistant Core (only) may call:

```yaml
rest_command:
  obsidian_note:
    url: http://4034a19f-livesync-bridge:8099/api/append
    method: POST
    content_type: application/json
    payload: '{{ {"text": text} | to_json }}'
```

Each call adds `- HH:MM text` to `<voice_note_folder>/YYYY-MM-DD.md`.
The hostname is shown on the add-on's Info page; it starts with your repository's ID.

## Access

The panel answers only through Home Assistant ingress, and the API only to Home Assistant Core.
Requests from anything else on the network are refused.
