# Third-Party Notices

**O.D.I.N. (Orchestrated Dispatch & Inventory Network)**

This file lists third-party software packages used by O.D.I.N. and their respective licenses. The packages in the dependency tables are used under their own licenses alongside O.D.I.N.'s Business Source License 1.1 (BSL 1.1).

## Separately licensed bundled components (not covered by the BSL)

These files ship with O.D.I.N. but are not part of its Licensed Work. They are licensed to you only under their own licenses, and nothing in O.D.I.N.'s BSL, Terms of Service or license keys restricts the rights those licenses give you.

| Component | File | License | Source |
|---|---|---|---|
| Obico spaghetti-detection model (unmodified; SHA-256 `0a6ebd8e30dbf6a450c50f9c0a5406f04ba7eb1c99fd5996e888c78bb383b9aa`) | `backend/vision_models_default/obico_spaghetti.onnx` | AGPL-3.0 (text: `backend/vision_models_default/LICENSE-AGPL-3.0.txt`) | Obico, formerly The Spaghetti Detective: https://github.com/TheSpaghettiDetective/obico-server/tree/1e7abb0bdf1c185b38a913d8bdbdbc3daa3df634/ml_api/model. Notice and source details: `backend/vision_models_default/NOTICE.txt`; mirror: https://github.com/HughKantsime/runsodin/releases/tag/third-party/obico-model-1e7abb0b |
| Bambu Lab HMS and print-error descriptions | `backend/data/bambu_hms/*.json` | Bambu Lab's text; see `NOTICE` | https://e.bambulab.com/query.php |

---

## Backend Dependencies (Python)

| Package | Version | License |
|---------|---------|---------|
| FastAPI | 0.115.6 | MIT |
| Uvicorn | 0.34.0 | BSD-3-Clause |
| python-multipart | 0.0.18 | Apache-2.0 |
| SQLAlchemy | 2.0.36 | MIT |
| Alembic | 1.14.1 | MIT |
| PyJWT | 2.9.0 | MIT |
| Passlib | 1.7.4 | BSD-3-Clause |
| cryptography | 44.0.0 | Apache-2.0 / BSD-3-Clause |
| Pydantic | 2.10.4 | MIT |
| pydantic-settings | 2.7.1 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| paho-mqtt | 2.1.0 | EPL-2.0 / EDL-1.0 |
| lxml | 6.1.0 | BSD-3-Clause |
| pywebpush | 2.0.1 | MPL-2.0 |
| Authlib | 1.4.1 | BSD-3-Clause |
| PyOTP | 2.9.0 | MIT |
| PyYAML | 6.0.2 | MIT |
| qrcode | latest | BSD-3-Clause |
| bcrypt | 4.2.1 | Apache-2.0 |
| fpdf2 | 2.8.3 | LGPL-3.0 |
| NumPy | <2 | BSD-3-Clause |
| opencv-python-headless | 4.9.0.80 | Apache-2.0 |
| ONNX Runtime | 1.17.1 | MIT |

---

## Frontend Dependencies (JavaScript)

### Runtime Dependencies

| Package | Version | License |
|---------|---------|---------|
| React | ^18.2.0 | MIT |
| React DOM | ^18.2.0 | MIT |
| React Router DOM | ^6.21.0 | MIT |
| @tanstack/react-query | ^5.17.0 | MIT |
| Recharts | ^3.7.0 | MIT |
| Lucide React | ^0.303.0 | ISC |
| clsx | ^2.1.0 | MIT |
| date-fns | ^3.2.0 | MIT |
| jsQR | ^1.4.0 | Apache-2.0 |
| react-hot-toast | ^2.6.0 | MIT |

### Development Dependencies

| Package | Version | License |
|---------|---------|---------|
| Vite | ^5.0.0 | MIT |
| @vitejs/plugin-react | ^4.2.0 | MIT |
| Tailwind CSS | ^3.4.1 | MIT |
| PostCSS | ^8.4.33 | MIT |
| Autoprefixer | ^10.4.16 | MIT |

---

## Bundled Services

| Component | License |
|-----------|---------|
| go2rtc | MIT |
| SQLite | Public domain |

---

## License Compatibility

The dependency licenses in the tables above (MIT, BSD-3-Clause, Apache-2.0, ISC, MPL-2.0, LGPL-3.0, EPL-2.0) allow use alongside O.D.I.N.'s BSL 1.1 license. This statement does not cover the separately licensed bundled components listed at the top of this file, such as the AGPL-3.0 Obico model, which keep their own licenses and are not part of O.D.I.N.'s Licensed Work. Copyleft obligations (LGPL-3.0 for fpdf2, MPL-2.0 for pywebpush) are satisfied through dynamic linking and unmodified use of those libraries.

---

*Last updated: November 7, 2026*


## Inter font (bundled locally)

The existing `@fontsource/inter` dependency supplies weights 400, 500, 600 and 700 for Fjord. No external font service is used.

```text
Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter) Inter-Italic[opsz,wght].ttf: Copyright 2016 The Inter Project Authors (https://github.com/rsms/inter)

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
http://scripts.sil.org/OFL


-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```
