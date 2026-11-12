# Launcher signature test vector

`manifest.json` is signed with a public test key (seed 0..31). Never use this key for a release.

`canonical.bin` is the exact Python canonical payload without the `key_id` prefix. The signed message is `key_id + "\0" + canonical.bin`.
