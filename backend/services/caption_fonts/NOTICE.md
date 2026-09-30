NotoEmoji-Bold.ttf

Google "Noto Emoji" — the MONOCHROME outline emoji font (not Noto Color
Emoji), from google/fonts `ofl/notoemoji/NotoEmoji[wght].ttf` at commit
b979dba422e445492b0eb9951ac52ee0b4d648c3 (upstream: googlefonts/emoji-bw
v3.000). Copyright 2013, 2022 Google LLC.

License: SIL Open Font License 1.1 — full text in OFL.txt next to this file,
which must ship with the font. OFL permits bundling with software and
modification; it declares no Reserved Font Name, so the modified font keeps
its name. The OFL is compatible with distribution alongside this project's
AGPL-3.0 code; the font itself stays under the OFL.

Modifications (fontTools 4.62.1): the variable font instantiated at wght=700
(`varLib.instancer`, 1.98 MB -> 885 KB), and the (3,1) BMP-only cmap subtable
removed. libass 0.17 selects the FIRST Microsoft Unicode cmap it finds, which
is the BMP-only one, so every emoji above U+FFFF (i.e. nearly all of them)
was "not found" and burned as a box; with it gone libass uses the (3,10)
full-Unicode table, which also covers the BMP.
Size: 885 KB. sha256: fb3581db29965140d5e228e7d7719c3fd04e61cf0e189ae292a1845fd74ba6ad

Why it exists: libass cannot draw colour-bitmap emoji fonts (Apple Color
Emoji / Noto Color Emoji), so burned-in AutoEmoji rendered as tofu. See
`EMOJI_FONT_*`, `emojify_ass_text` and `ass_filter` in
backend/services/caption_service.py. Shipped in the desktop bundle by
`collect_data_files("backend")` in desktop/scripts/viralmint.spec. If the file
is missing, emoji are left out of burned captions rather than burned as boxes.
