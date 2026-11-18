# Obsidian LiveSync Bridge

Runs [livesync-bridge](https://github.com/vrtmrz/livesync-bridge) by vrtmrz next to Home Assistant.
It keeps the CouchDB database of the Obsidian **Self-hosted LiveSync** plugin in two-way sync with
an ordinary folder of Markdown files in `/share`, for example `/share/obsidian`.

Once the folder is there, Home Assistant (a `shell_command`, a script, a voice assistant) can read
notes and add new ones simply by writing `.md` files, and they show up in Obsidian on every device.
Writing straight into CouchDB is not a good idea: LiveSync splits every note into chunks and may
encrypt them, which this bridge handles for you.

## Configuration

| Option | What it is |
|---|---|
| `couchdb_url` | CouchDB address, the same one the LiveSync plugin uses, e.g. `http://192.168.1.2:5984` |
| `database` | Database name from the plugin settings |
| `username`, `password` | CouchDB user from the plugin settings |
| `passphrase` | End-to-end encryption passphrase, if *End-to-End Encryption* is on in the plugin. Leave empty otherwise |
| `path_obfuscation` | Turn on if *Path Obfuscation* is on in the plugin (needs the passphrase) |
| `share_folder` | Folder inside `/share` for the files, default `obsidian` → `/share/obsidian` |
| `vault_subfolder` | Sync only this folder of the vault (e.g. `Inbox`). Empty = the whole vault |
| `unicode_nfc_paths` | Must match the vault's path normalisation. Leave off for an existing vault unless you know it uses NFC |
| `filename_case_sensitive` | Must match *Handle files as Case-Sensitive* in the plugin |
| `reset_on_start` | Re-read the database and the folder from the beginning on the next start. Turn it off again afterwards |

Chunk size and other remote settings are taken from the database automatically.

## Notes

- The first start downloads the whole vault (or the chosen subfolder) into the folder.
- Files are synced both ways; deleting a file in the folder deletes the note in the vault.
- Sync state is kept in the add-on's `/data`, so restarts continue where they stopped.
- Back up the vault before the first start, as with any new sync client.
