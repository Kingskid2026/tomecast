# Changelog

## [0.2.1](https://github.com/Kingskid2026/tomecast/compare/v0.2.0...v0.2.1) (2026-11-11)


### Documentation

* mark 0.2.0 as released in the README roadmap ([#78](https://github.com/Kingskid2026/tomecast/issues/78)) ([daed18e](https://github.com/Kingskid2026/tomecast/commit/daed18eadcd774a33d1b3541c27f298e15c9834c))

## [0.2.0](https://github.com/Kingskid2026/tomecast/compare/v0.1.2...v0.2.0) (2026-11-11)


### Features

* a Names step in the window to hear and fix how names are said ([#45](https://github.com/Kingskid2026/tomecast/issues/45)) ([f54c47e](https://github.com/Kingskid2026/tomecast/commit/f54c47ead4d04c14dab1791e574c3805a2ca98ea))
* carry on on the CPU with the same voice when the graphics card runs out of memory ([#55](https://github.com/Kingskid2026/tomecast/issues/55)) ([9a4dedd](https://github.com/Kingskid2026/tomecast/commit/9a4deddc2342c306ac54be3e76539398443e7b2c))
* install the sentence-grid app icon, with PNG sizes ([#44](https://github.com/Kingskid2026/tomecast/issues/44)) ([5be1914](https://github.com/Kingskid2026/tomecast/commit/5be19143c6a4a3fd1060118b3624d899cf0aeafc))
* run the voice engine on distros older than glibc 2.43 ([#27](https://github.com/Kingskid2026/tomecast/issues/27)) ([97344d0](https://github.com/Kingskid2026/tomecast/commit/97344d01d69809f3143cd0c6d0e9c9f089326afa))
* switch engine builds only after a self-check, and measure the speed ([#47](https://github.com/Kingskid2026/tomecast/issues/47)) ([25bbf97](https://github.com/Kingskid2026/tomecast/commit/25bbf97372973be2225f83f627b45dec85cd24a2))


### Bug Fixes

* check the convert output before voicing, find uv where it went, and run real conversions for every CLI change ([#72](https://github.com/Kingskid2026/tomecast/issues/72)) ([2828581](https://github.com/Kingskid2026/tomecast/commit/2828581388cc9724818c08e5a2b1eb3c06bcda1f))
* free the graphics card when it's switched off, and keep the first click on a fix ([#71](https://github.com/Kingskid2026/tomecast/issues/71)) ([b3d55b8](https://github.com/Kingskid2026/tomecast/commit/b3d55b8ed4421e7e7c7194fab16578db3ff8d5b9))
* keep the app icon crisp at 16 and 24 px ([#48](https://github.com/Kingskid2026/tomecast/issues/48)) ([24c0789](https://github.com/Kingskid2026/tomecast/commit/24c078903dc5ddc46a619bc6c92142c6c7fb87d3))
* keep the time-stretch for books made in the window above 1.5x ([#74](https://github.com/Kingskid2026/tomecast/issues/74)) ([a0298f6](https://github.com/Kingskid2026/tomecast/commit/a0298f60668a1ce2b70a33ccdee2e76c30d9518e))
* keep words with ligatures whole when reading PDFs ([#75](https://github.com/Kingskid2026/tomecast/issues/75)) ([af7ab98](https://github.com/Kingskid2026/tomecast/commit/af7ab98288765a74ce56f1dfcc89770aa50c4d02))
* let the book's pronunciation list win in every case ([#46](https://github.com/Kingskid2026/tomecast/issues/46)) ([3bac0e1](https://github.com/Kingskid2026/tomecast/commit/3bac0e1f5209660c5ca7b7d8bf7e76f4c54ca442))
* measure speed and time left on voicing alone, stretch CLI previews, keep the card's name ([#69](https://github.com/Kingskid2026/tomecast/issues/69)) ([9d49a85](https://github.com/Kingskid2026/tomecast/commit/9d49a85430aba4f211bfe575c3e11bda2ae7c4fd))
* re-voice damaged chunks, free the install lock while asking, fix version rollbacks ([#73](https://github.com/Kingskid2026/tomecast/issues/73)) ([3ae104b](https://github.com/Kingskid2026/tomecast/commit/3ae104bc8a0582f32fcf440e9246b30ddaac93a6))
* read private-use fi and fl ligatures, and pin the same-glyph rule ([#76](https://github.com/Kingskid2026/tomecast/issues/76)) ([bbe1f07](https://github.com/Kingskid2026/tomecast/commit/bbe1f076f9ae44809e74446187f1b00d5d9e49dc))
* refuse IPA in pronunciation lists, not only /slashes/ ([#64](https://github.com/Kingskid2026/tomecast/issues/64)) ([f81c56d](https://github.com/Kingskid2026/tomecast/commit/f81c56dde77b00d95973173d5c87e23e82695a1a))
* roll back failed engine updates, and other review fixes for [#47](https://github.com/Kingskid2026/tomecast/issues/47) ([#50](https://github.com/Kingskid2026/tomecast/issues/50)) ([d799221](https://github.com/Kingskid2026/tomecast/commit/d799221b232d352b9a1868e39594d1b64c693ce4))
* second round of review fixes for engine switching ([#51](https://github.com/Kingskid2026/tomecast/issues/51)) ([154a5dd](https://github.com/Kingskid2026/tomecast/commit/154a5dd99b32a1a5aa2ee0b47657617698f3501b))
* stop the engine without segfaults, say when a restart left the card, quit on an early Ctrl-C ([#70](https://github.com/Kingskid2026/tomecast/issues/70)) ([9559d12](https://github.com/Kingskid2026/tomecast/commit/9559d1207cccac3a5673e88e2d5bde82c8b09fd5))
* tidy up engine switching and the graphics-card fallback ([#53](https://github.com/Kingskid2026/tomecast/issues/53)) ([#66](https://github.com/Kingskid2026/tomecast/issues/66)) ([b822231](https://github.com/Kingskid2026/tomecast/commit/b822231ba9c3b4db55845b6725d5c07597f6dd46))


### Documentation

* bring the README up to date for 0.2.0 ([#56](https://github.com/Kingskid2026/tomecast/issues/56)) ([5400542](https://github.com/Kingskid2026/tomecast/commit/5400542809ae1a9279936ca0abc5b8f60e72e526))
* drop the code of conduct, and the 'paused' note ([#42](https://github.com/Kingskid2026/tomecast/issues/42)) ([02493a1](https://github.com/Kingskid2026/tomecast/commit/02493a17d90581b48763d44147a545f88a9ed96b))
* say what happens when the graphics card runs out of memory ([#60](https://github.com/Kingskid2026/tomecast/issues/60)) ([c70294f](https://github.com/Kingskid2026/tomecast/commit/c70294f686cbd3297b42f42ddfaed9bc002ceeff))

## [0.1.2](https://github.com/Kingskid2026/tomecast/compare/v0.1.1...v0.1.2) (2026-11-05)


### Features

* MP3 output, one file per chapter ([#35](https://github.com/Kingskid2026/tomecast/issues/35)) ([7086ca2](https://github.com/Kingskid2026/tomecast/commit/7086ca2dbbef1b219a264e7653ffd8c079a14dff))
* read PDF books and clean up the page furniture ([#37](https://github.com/Kingskid2026/tomecast/issues/37)) ([2f8e3d0](https://github.com/Kingskid2026/tomecast/commit/2f8e3d02f43d702adff20c32a93ee37ecc4eaab9))
* say numbers, abbreviations and names right ([#36](https://github.com/Kingskid2026/tomecast/issues/36)) ([861927e](https://github.com/Kingskid2026/tomecast/commit/861927e7dc3864a0a70da729961ced26e4bc8a29))


### Documentation

* mark Tomecast as a preview and lay out the roadmap to 1.0 ([#41](https://github.com/Kingskid2026/tomecast/issues/41)) ([86c5bd2](https://github.com/Kingskid2026/tomecast/commit/86c5bd26ab25e881bbc5b87e2a436d47973c3deb))

## [0.1.1](https://github.com/Kingskid2026/tomecast/compare/v0.1.0...v0.1.1) (2026-11-05)


### Features

* read DOCX, FB2, TXT and HTML books ([#31](https://github.com/Kingskid2026/tomecast/issues/31)) ([058d162](https://github.com/Kingskid2026/tomecast/commit/058d162bd2b20c9139efb73a607f9dd954b6eb39))


### Bug Fixes

* start the voice engine on a fresh port if its port was taken ([#33](https://github.com/Kingskid2026/tomecast/issues/33)) ([63c7ae4](https://github.com/Kingskid2026/tomecast/commit/63c7ae4fbfffcc862dc7f40f9c12f23005760de2))
* the installer always finds the newest release ([#29](https://github.com/Kingskid2026/tomecast/issues/29)) ([3439509](https://github.com/Kingskid2026/tomecast/commit/34395098ad33102ab9e9f3ba53243d034b623ac8))
* the window quits cleanly on SIGTERM and Ctrl-C ([#30](https://github.com/Kingskid2026/tomecast/issues/30)) ([a9300c3](https://github.com/Kingskid2026/tomecast/commit/a9300c350644b9de7c5513474cfc9bb66b6cddff))

## [0.1.0](https://github.com/Kingskid2026/tomecast/compare/v0.0.2...v0.1.0) (2026-11-05)


### Features

* desktop window ([#26](https://github.com/Kingskid2026/tomecast/issues/26)) ([591c743](https://github.com/Kingskid2026/tomecast/commit/591c74396f66e895d5303d26f01c2e33a8debb12))
* use the graphics card through the installed driver ([#28](https://github.com/Kingskid2026/tomecast/issues/28)) ([ed06533](https://github.com/Kingskid2026/tomecast/commit/ed06533bbab73e7c0efa0d9fcba930d6ffa32434))


### Documentation

* design updates from the mockup review (art, dots, resizing) ([#24](https://github.com/Kingskid2026/tomecast/issues/24)) ([63727d5](https://github.com/Kingskid2026/tomecast/commit/63727d53d70c697d0a49c3a7200498a721933970))
* point the one-line install at a URL that works ([#25](https://github.com/Kingskid2026/tomecast/issues/25)) ([f7a9a25](https://github.com/Kingskid2026/tomecast/commit/f7a9a258304a15549ff795811f2a9eea7ca8a1b4))
* record the Night Studio GUI design and who owns it ([#20](https://github.com/Kingskid2026/tomecast/issues/20)) ([83a4d1f](https://github.com/Kingskid2026/tomecast/commit/83a4d1f66805cdf9d154d294a6ffc4b9c66e1da4))

## [0.0.2](https://github.com/Kingskid2026/tomecast/compare/v0.0.1...v0.0.2) (2026-11-05)


### Features

* convert EPUB books to M4B audiobooks with Kokoro ([#19](https://github.com/Kingskid2026/tomecast/issues/19)) ([3830d54](https://github.com/Kingskid2026/tomecast/commit/3830d54c137b8e0e0220b974d3c75c9c7848b69d))

## 0.0.1 (2026-11-05)


### Features

* initial project setup with a `tomecast --version` command ([ebe60d5](https://github.com/Kingskid2026/tomecast/commit/ebe60d52e70efb26a50ce2f727478aa6ce7a0f4c))


### Continuous Integration

* pin the first release to 0.0.1 ([#17](https://github.com/Kingskid2026/tomecast/issues/17)) ([b23e5e7](https://github.com/Kingskid2026/tomecast/commit/b23e5e755c90564bd3213262f16ddb5cebce484f))
