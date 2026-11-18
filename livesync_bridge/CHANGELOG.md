# Changelog

## 0.2.0

- **Obsidian** sidebar panel (ingress): browse, read, edit, create and delete notes straight in CouchDB.
- `POST /api/append` for Home Assistant voice notes into a daily note (`voice_note_folder`).
- The plain-text mirror in `/share` is now optional (`mirror_to_share`, off by default), so the vault is
  no longer copied to disk unless you ask for it.

## 0.1.1

- Base image set in the Dockerfile (build.yaml is deprecated in Supervisor).

## 0.1.0

- First version: livesync-bridge (upstream commit 1c25230) between a LiveSync CouchDB vault and `/share/<folder>`.
