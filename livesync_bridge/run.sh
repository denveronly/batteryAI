#!/bin/sh
set -e
cd /app
opt() { deno eval "console.log(JSON.parse(Deno.readTextFileSync('/data/options.json'))['$1'])"; }

# Deno keeps localStorage (the mirror's resume positions and scan state) under $DENO_DIR/location_data.
mkdir -p /data/location_data
rm -rf /deno-dir/location_data
ln -s /data/location_data /deno-dir/location_data

if [ "$(opt mirror_to_share)" = "true" ]; then
  deno run --allow-read=/data,/share --allow-write=/data,/share /gen_config.ts
  if [ "$(opt reset_on_start)" = "true" ]; then
    echo "reset_on_start is on: the mirror scans the folder and the database from the beginning"
    deno run -A main.ts --reset &
  else
    deno run -A main.ts &
  fi
else
  # No copy of the vault on disk. Forget the mirror's scan state, so that turning the mirror on
  # later starts clean instead of treating the missing files as deletions.
  rm -rf /data/location_data/* /data/config.json
  echo "Mirror to /share is off: notes stay only in CouchDB"
fi

exec deno run -A notes_server.ts
