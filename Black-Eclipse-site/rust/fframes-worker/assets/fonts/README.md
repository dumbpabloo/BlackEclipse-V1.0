# Bundled fonts (video worker)

These faces are **linked into `fframes-worker`** and registered with the renderer
(`src/fonts.rs` → `DynamicMediaProvider`), so layout measurement and drawing use
the same face on every operating system. The worker no longer depends on the
host's `Segoe UI` / `Consolas`.

| File | Family | Weight | Source |
|---|---|---|---|
| `Inter-Regular.ttf` | Inter 4.1 | 400 | https://github.com/rsms/inter |
| `Inter-Medium.ttf` | Inter 4.1 | 500 | https://github.com/rsms/inter |
| `Inter-SemiBold.ttf` | Inter 4.1 | 600 | https://github.com/rsms/inter |
| `Inter-Bold.ttf` | Inter 4.1 | 700 | https://github.com/rsms/inter |
| `JetBrainsMono-Regular.ttf` | JetBrains Mono 2.304 | 400 | https://github.com/JetBrains/JetBrainsMono |
| `JetBrainsMono-Bold.ttf` | JetBrains Mono 2.304 | 700 | https://github.com/JetBrains/JetBrainsMono |

Both families are licensed under the **SIL Open Font License 1.1**; the full
texts ship beside the binaries as `Inter-OFL.txt` and `JetBrainsMono-OFL.txt`.
The OFL permits bundling and redistribution, including inside a compiled
application, provided the licence accompanies the font.

System fonts remain enabled as a *glyph* fallback (CJK, emoji). They are not the
primary face, so Latin layout is deterministic.
