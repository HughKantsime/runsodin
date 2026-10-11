# Invoice fonts

Local SIL Open Font License 1.1 fonts avoid rendering network calls and host font dependencies. Licenses are retained alongside the assets. Noto Sans regular/bold cover tested Latin, Greek and Cyrillic. Noto Sans SC regular/bold are the CJK fallback. This is bounded coverage, not a claim of every Unicode script or emoji.

`latin-source.json` pins upstream revision, download URLs and hashes. `cjk-ttf-source.json` pins the original variable TrueType source and the generated static 400/700 weight files. Derivation uses fontTools.varLib.instancer; all source glyphs retained. TrueType is used because the application's pinned fpdf2 2.8.3 generated a PDF font-type warning with CFF OpenType sources in Poppler.

Font embedding/fallback: https://py-pdf.github.io/fpdf2/Unicode.html
Upstream: https://github.com/notofonts/noto-fonts and https://github.com/notofonts/noto-cjk

Invoice text outside the packaged fonts is rejected with a safe 422; no silent missing glyphs. Full script shaping and arbitrary emoji are not certified.
