# Bundled fonts

Static font files for the Tomecast desktop window, all under the SIL Open Font License 1.1.
Static files are used because Qt handles variable axes unreliably.

All sources are in [google/fonts](https://github.com/google/fonts) at commit
`fbe81817505e8aeadc53cc7a38a2b76fb57c333e`. Instances were made with fontTools
(`uvx --from fonttools fonttools varLib.instancer`, run on 2026-11-05).

| File | Use | Licence file |
|---|---|---|
| `Doto-Bold.ttf` | Big numerals (`02:10:34`, `41%`), 24 px and up | `OFL-Doto.txt` |
| `IBMPlexSans-Regular.ttf`, `IBMPlexSans-Medium.ttf` | Interface text | `OFL-IBMPlex.txt` |
| `IBMPlexMono-Regular.ttf`, `IBMPlexMono-Medium.ttf` | Labels, data, live caption | `OFL-IBMPlex.txt` |

`OFL-IBMPlex.txt` is byte-identical for Plex Sans and Plex Mono in the source repo.
`OFL-Doto.txt` and `OFL-IBMPlex.txt` are copied unchanged from `ofl/doto/OFL.txt` and `ofl/ibmplexsans/OFL.txt`.

## Files

### Doto-Bold.ttf

| | |
|---|---|
| Upstream | https://raw.githubusercontent.com/google/fonts/fbe81817505e8aeadc53cc7a38a2b76fb57c333e/ofl/doto/Doto%5BROND,wght%5D.ttf |
| Upstream SHA-256 | `6f4fe7d37853b91df3698daa84cde2dbe1c9695d88c986e6510134910337d426` |
| Command | `uvx --from fonttools fonttools varLib.instancer 'Doto[ROND,wght].ttf' wght=700 ROND=100 -o Doto-round.ttf`, then the naming step below |
| Output SHA-256 | `49b91c57049d879b08afb5ed9eae20262093d90326d5175644ee7ca0dfe0b607` |
| Names inside | family (ID 1) `Doto`, style (ID 2) `Bold`, full name `Doto Bold`, weight class 700 |

`ROND=100` gives **round dots**, so the numerals match every other dot in the window (the art, the sentence grid, the meters). The axis runs from 0 (square dots, the font's default) to 100. Its default `wght` is 900; 700 is used.

`--update-name-table` can't be used here, because the font's STAT table has no entry for `ROND=100`. Instead, a short fontTools step copies the naming records 1, 2, 3, 4, 6, 16 and 17 from the earlier `Doto-Bold.ttf` (family `Doto`, style `Bold`), sets `OS/2.usWeightClass` to 700, copies `fsSelection` and `macStyle`, and drops the leftover STAT table (a static file has no axes to describe). The Doto licence has no Reserved Font Name, so a modified instance may keep the name.

### IBMPlexSans-Regular.ttf

| | |
|---|---|
| Upstream | https://raw.githubusercontent.com/google/fonts/fbe81817505e8aeadc53cc7a38a2b76fb57c333e/ofl/ibmplexsans/IBMPlexSans%5Bwdth,wght%5D.ttf |
| Upstream SHA-256 | `3b031aa4216174205bd8471f88a49b91f093169e9e87bd5262242bc5967fe2e3` |
| Command | `uvx --from fonttools fonttools varLib.instancer 'IBMPlexSans[wdth,wght].ttf' wght=400 wdth=100 --update-name-table -o IBMPlexSans-Regular.ttf` |
| Output SHA-256 | `322719b812efe595682e44568de165d3848a58d991c6ef23fe46a40a8b600e49` |
| Names inside | family `IBM Plex Sans`, style `Regular`, weight class 400 |

### IBMPlexSans-Medium.ttf

| | |
|---|---|
| Upstream | https://raw.githubusercontent.com/google/fonts/fbe81817505e8aeadc53cc7a38a2b76fb57c333e/ofl/ibmplexsans/IBMPlexSans%5Bwdth,wght%5D.ttf |
| Upstream SHA-256 | `3b031aa4216174205bd8471f88a49b91f093169e9e87bd5262242bc5967fe2e3` |
| Command | `uvx --from fonttools fonttools varLib.instancer 'IBMPlexSans[wdth,wght].ttf' wght=500 wdth=100 --update-name-table -o IBMPlexSans-Medium.ttf` |
| Output SHA-256 | `cfd2e1ab3e73c75325fe7250b0060746bead8a4bc86e38e90001e28447e786fe` |
| Names inside | ID 1 `IBM Plex Sans Medium`, ID 2 `Regular`, typographic family (ID 16) `IBM Plex Sans`, typographic style (ID 17) `Medium`, weight class 500 |

### IBMPlexMono-Regular.ttf

| | |
|---|---|
| Upstream | https://raw.githubusercontent.com/google/fonts/fbe81817505e8aeadc53cc7a38a2b76fb57c333e/ofl/ibmplexmono/IBMPlexMono-Regular.ttf |
| Upstream SHA-256 | `6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46` |
| Command | none (upstream static file, copied unchanged) |
| Output SHA-256 | `6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46` |
| Names inside | family `IBM Plex Mono`, style `Regular`, weight class 400 |

### IBMPlexMono-Medium.ttf

| | |
|---|---|
| Upstream | https://raw.githubusercontent.com/google/fonts/fbe81817505e8aeadc53cc7a38a2b76fb57c333e/ofl/ibmplexmono/IBMPlexMono-Medium.ttf |
| Upstream SHA-256 | `a9b4c49bb299e05b5f6c481e7fb5e78943d2793249a0c8874ab574a2d1ea6755` |
| Command | none (upstream static file, copied unchanged) |
| Output SHA-256 | `a9b4c49bb299e05b5f6c481e7fb5e78943d2793249a0c8874ab574a2d1ea6755` |
| Names inside | ID 1 `IBM Plex Mono Medium`, ID 2 `Regular`, typographic family (ID 16) `IBM Plex Mono`, typographic style (ID 17) `Medium`, weight class 500 |

## Checks

Every file has no `fvar` table and contains glyphs for `0-9 : · × — ‘ ’ “ ” [ ] %`.
Qt (`QFontDatabase.addApplicationFont`) reports the families `Doto`, `IBM Plex Sans` and `IBM Plex Mono`
(the Medium files also register a `... Medium` legacy family name).
