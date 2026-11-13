# Read-only ext4 parser sources

`extfs-sources.zip` contains the complete Python sources and original license
notices of these pinned projects. They are used only to read full E5 firmware
images while generating a differential OTA; they do not mount filesystems.

* [dissect.extfs 3.15](https://github.com/fox-it/dissect.extfs): AGPL-3.0-or-later.
* [dissect.cstruct 4.7](https://github.com/fox-it/dissect.cstruct): Apache-2.0.
* [dissect.util 3.24](https://github.com/fox-it/dissect.util): Apache-2.0.

Corresponding source and license files are included in the archive, not only
bytecode. See `extfs-sources.json` for individual checksums and version/source
URLs. Reproduce it using `tools/vendor-ota-extfs.py` with the listed distributions
installed. No pip operation is performed on the device. Preserve these notices
when redistributing the generator.
