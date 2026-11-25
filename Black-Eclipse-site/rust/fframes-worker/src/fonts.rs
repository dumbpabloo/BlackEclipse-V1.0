//! Bundled type system for every rendered frame.
//!
//! The worker used to resolve `Segoe UI` / `Consolas` from the host OS. That made
//! a render depend on the machine: a Linux or macOS host silently substituted a
//! different face, glyph metrics shifted, and text that fit on Windows could clip
//! elsewhere. The layout engine's measurements are taken from the SAME face it
//! draws with, so the fix is to ship the faces.
//!
//! Both families are SIL Open Font License 1.1 and are vendored under
//! `assets/fonts/` with their licence files:
//!   - Inter 4.1 (rsms/inter) — UI sans; Regular/Medium/SemiBold/Bold.
//!   - JetBrains Mono 2.304 (JetBrains/JetBrainsMono) — numerals and source lines.
//!
//! They are registered through fframes' `DynamicMediaProvider`, i.e. as binary
//! font data linked into the worker, exactly the way the crate recommends. System
//! fonts stay enabled as a GLYPH fallback (CJK, emoji) — not as the primary face.

use std::sync::Arc;

use fframes::{DynamicMediaProvider, RawFontData};

/// The primary UI face (matches `theme::SANS`).
pub const SANS: &str = "Inter";
/// The numeral/source face (matches `theme::MONO`).
pub const MONO: &str = "JetBrains Mono";

const INTER_REGULAR: &[u8] = include_bytes!("../assets/fonts/Inter-Regular.ttf");
const INTER_MEDIUM: &[u8] = include_bytes!("../assets/fonts/Inter-Medium.ttf");
const INTER_SEMIBOLD: &[u8] = include_bytes!("../assets/fonts/Inter-SemiBold.ttf");
const INTER_BOLD: &[u8] = include_bytes!("../assets/fonts/Inter-Bold.ttf");
const MONO_REGULAR: &[u8] = include_bytes!("../assets/fonts/JetBrainsMono-Regular.ttf");
const MONO_BOLD: &[u8] = include_bytes!("../assets/fonts/JetBrainsMono-Bold.ttf");

fn raw(name: &str, bytes: &'static [u8]) -> RawFontData {
    RawFontData {
        file_name: name.to_owned(),
        data: Arc::new(bytes),
    }
}

/// A provider carrying only the bundled fonts (renders have no audio/images).
pub fn media_provider() -> DynamicMediaProvider<'static> {
    DynamicMediaProvider {
        fontdata: vec![
            raw("Inter-Regular.ttf", INTER_REGULAR),
            raw("Inter-Medium.ttf", INTER_MEDIUM),
            raw("Inter-SemiBold.ttf", INTER_SEMIBOLD),
            raw("Inter-Bold.ttf", INTER_BOLD),
            raw("JetBrainsMono-Regular.ttf", MONO_REGULAR),
            raw("JetBrainsMono-Bold.ttf", MONO_BOLD),
        ],
        ..Default::default()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_bundled_face_parses_as_a_real_font() {
        for (name, bytes) in [
            ("Inter-Regular", INTER_REGULAR),
            ("Inter-Medium", INTER_MEDIUM),
            ("Inter-SemiBold", INTER_SEMIBOLD),
            ("Inter-Bold", INTER_BOLD),
            ("JetBrainsMono-Regular", MONO_REGULAR),
            ("JetBrainsMono-Bold", MONO_BOLD),
        ] {
            let face = fframes::ttf_parser::Face::parse(bytes, 0)
                .unwrap_or_else(|error| panic!("{name} is not a parseable TTF: {error}"));
            assert!(face.units_per_em() > 0);
        }
    }

    #[test]
    fn the_provider_registers_every_face() {
        assert_eq!(media_provider().fontdata.len(), 6);
    }
}
