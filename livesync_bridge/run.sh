#!/bin/sh
set -e
cd /app

# Deno keeps localStorage (resume positions, offline-scan state) under $DENO_DIR/location_data.
# Keep it in /data so it survives add-on restarts and updates.
mkdir -p /data/location_data
rm -rf /deno-dir/location_data
ln -s /data/location_data /deno-dir/location_data

deno run --allow-read=/data,/share --allow-write=/data,/share /gen_config.ts

if [ "$(deno eval 'console.log(JSON.parse(Deno.readTextFileSync("/data/options.json")).reset_on_start)')" = "true" ]; then
  echo "reset_on_start is on: scanning the folder and the database from the beginning"
  echo "(turn the option off again after this start)"
  exec deno run -A main.ts --reset
fi
exec deno run -A main.ts
