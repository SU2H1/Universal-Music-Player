//! Fonts and colours of the player (2.0, `docs/player-spec.md`): SF Pro from
//! the system on macOS (bold for headings, "SF Pro Display Bold"), the bundled
//! Inter (SIL Open Font License) on Windows and Linux, or the family chosen in
//! Settings → Appearance; light and dark palettes close to Apple Music's.
//! Japanese (2.1: the menus in Japanese, and Japanese titles in any language)
//! is drawn with an installed Japanese font, regular and bold, added after
//! those (`japanese_fonts()`); no CJK font is bundled.

use std::sync::{Arc, OnceLock, RwLock};

use egui::{Color32, FontData, FontDefinitions, FontFamily, FontId, Stroke, TextStyle};

/// The variable Inter, with its licence in `realhires/assets/fonts/`.
const INTER: &[u8] = include_bytes!("../../../../realhires/assets/fonts/InterVariable.ttf");
const SF_PRO: &str = "/System/Library/Fonts/SFNS.ttf";

/// The family name of the bold heading font.
pub const BOLD: &str = "rhr-bold";

pub fn bold() -> FontFamily {
    FontFamily::Name(BOLD.into())
}

/// Heading sizes.
pub fn title(size: f32) -> FontId {
    FontId::new(size, bold())
}

pub fn text(size: f32) -> FontId {
    FontId::proportional(size)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Palette {
    pub bg: Color32,
    pub sidebar: Color32,
    pub text: Color32,
    pub secondary: Color32,
    pub separator: Color32,
    pub accent: Color32,
    pub lcd: Color32,
    pub hover: Color32,
    pub dark: bool,
}

fn hex(c: u32) -> Color32 {
    crate::style::hex(c)
}

pub fn palette(dark: bool) -> Palette {
    if dark {
        Palette {
            bg: hex(0x1e1e1e),
            sidebar: hex(0x262628),
            text: hex(0xf5f5f7),
            secondary: hex(0xa1a1a6),
            separator: hex(0x3a3a3c),
            accent: hex(0xfa2d48),
            lcd: hex(0x2c2c2e),
            hover: hex(0x333336),
            dark,
        }
    } else {
        Palette {
            bg: hex(0xffffff),
            sidebar: hex(0xf5f5f7),
            text: hex(0x1d1d1f),
            secondary: hex(0x6e6e73),
            separator: hex(0xd2d2d7),
            accent: hex(0xfa2d48),
            lcd: hex(0xf2f2f4),
            hover: hex(0xe8e8ed),
            dark,
        }
    }
}

/// The palettes in use for light and dark mode (set by `install_theme`).
static ACTIVE: RwLock<Option<(Palette, Palette)>> = RwLock::new(None);

pub fn current(ctx: &egui::Context) -> Palette {
    let dark = ctx.global_style().visuals.dark_mode;
    match *ACTIVE.read().unwrap() {
        Some((l, d)) => {
            if dark {
                d
            } else {
                l
            }
        }
        None => palette(dark),
    }
}

/// "#rrggbb" -> colour (black when it cannot be read).
pub fn parse_hex(v: &str) -> Color32 {
    let v = v.trim_start_matches('#');
    u32::from_str_radix(v, 16).map(hex).unwrap_or(Color32::BLACK)
}

/// `a` moved towards `b` by `t` (0..1).
pub fn mix(a: Color32, b: Color32, t: f32) -> Color32 {
    let l = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    Color32::from_rgb(l(a.r(), b.r()), l(a.g(), b.g()), l(a.b(), b.b()))
}

pub fn to_hex(c: Color32) -> String {
    format!("#{:02x}{:02x}{:02x}", c.r(), c.g(), c.b())
}

pub fn palette_of(t: &realhires::themes::Theme) -> Palette {
    let c = |k: &str| parse_hex(t.color(k));
    Palette {
        bg: c("background"),
        sidebar: c("sidebar"),
        text: c("text"),
        secondary: c("secondary"),
        separator: c("separator"),
        accent: c("accent"),
        lcd: c("panel"),
        hover: c("hover"),
        dark: t.dark,
    }
}

/// The installed font families (for the menu in Settings), read once.
pub fn installed_families() -> Vec<String> {
    static FAMILIES: OnceLock<Vec<String>> = OnceLock::new();
    FAMILIES
        .get_or_init(|| {
            let mut c = fontique::Collection::new(fontique::CollectionOptions { shared: false, system_fonts: true });
            let mut v: Vec<String> =
                c.family_names().filter(|n| !n.starts_with('.')).map(str::to_string).collect();
            v.sort_by_key(|n| n.to_lowercase());
            v.dedup();
            v
        })
        .clone()
}

fn tweak(weight: f32, opsz: Option<f32>) -> egui::FontTweak {
    let mut coords = egui::epaint::text::VariationCoords::new([(b"wght", weight)]);
    if let Some(o) = opsz {
        coords.push(b"opsz", o);
    }
    egui::FontTweak { coords, ..Default::default() }
}

/// (regular, bold) font data for `family` ("" = SF Pro on macOS, Inter elsewhere).
fn font_pair(family: &str) -> (FontData, FontData, String) {
    if !family.is_empty() {
        let mut c = fontique::Collection::new(fontique::CollectionOptions { shared: false, system_fonts: true });
        if let Some(fam) = c.family_by_name(family) {
            let pick = |w: fontique::FontWeight| {
                fam.match_font(fontique::FontWidth::NORMAL, fontique::FontStyle::Normal, w, false).and_then(|f| {
                    let blob = f.load(None)?;
                    let data = FontData::from_blob(Arc::new(blob.data().to_vec()), f.index());
                    let variable = f.has_weight_axis();
                    Some(if variable { data.tweak(tweak(w.value(), None)) } else { data })
                })
            };
            if let (Some(r), Some(b)) = (pick(fontique::FontWeight::NORMAL), pick(fontique::FontWeight::BOLD)) {
                return (r, b, family.to_string());
            }
        }
    }
    if cfg!(target_os = "macos") {
        if let Ok(bytes) = std::fs::read(SF_PRO) {
            let bytes = Arc::new(bytes);
            let r = FontData::from_blob(bytes.clone(), 0).tweak(tweak(400.0, Some(17.0)));
            let b = FontData::from_blob(bytes, 0).tweak(tweak(700.0, Some(28.0)));
            return (r, b, "SF Pro".into());
        }
    }
    let r = FontData::from_static(INTER).tweak(tweak(400.0, Some(14.0)));
    let b = FontData::from_static(INTER).tweak(tweak(700.0, Some(32.0)));
    (r, b, "Inter".into())
}

/// Families with Japanese kana and kanji, tried in this order: macOS,
/// Windows, then Linux; the system's own fallback for Japanese comes after.
const JAPANESE_FAMILIES: [&str; 12] = [
    "Hiragino Sans",
    "Hiragino Kaku Gothic ProN",
    "Yu Gothic UI",
    "Yu Gothic",
    "Meiryo UI",
    "Meiryo",
    "MS Gothic",
    "Noto Sans CJK JP",
    "Noto Sans JP",
    "IPAGothic",
    "TakaoGothic",
    "VL Gothic",
];

/// (regular, bold, family) of an installed font that draws Japanese, read once.
/// egui only draws the fonts it was given; eframe's system font provider
/// would find one character by character, but in the system's locale (so
/// kanji could come from a Chinese font) and only in a regular weight, so
/// headings in Japanese would not be bold.
pub fn japanese_fonts() -> Option<(FontData, FontData, String)> {
    static FOUND: OnceLock<Option<(FontData, FontData, String)>> = OnceLock::new();
    FOUND.get_or_init(find_japanese_fonts).clone()
}

fn find_japanese_fonts() -> Option<(FontData, FontData, String)> {
    let mut c = fontique::Collection::new(fontique::CollectionOptions { shared: false, system_fonts: true });
    let mut names: Vec<String> = JAPANESE_FAMILIES.iter().map(|s| s.to_string()).collect();
    if let Ok(ja) = fontique::Language::parse("ja") {
        for script in [*b"Hira", *b"Hani"] {
            let ids: Vec<fontique::FamilyId> = c.fallback_families((fontique::Script::from_bytes(script), &ja)).collect();
            for id in ids {
                if let Some(n) = c.family_name(id) {
                    names.push(n.to_string());
                }
            }
        }
    }
    for name in names {
        let Some(fam) = c.family_by_name(&name) else { continue };
        let face = |w: fontique::FontWeight| -> Option<FontData> {
            let f = fam.match_font(fontique::FontWidth::NORMAL, fontique::FontStyle::Normal, w, false)?;
            let blob = f.load(None)?; // memory-mapped, not copied
            let map = f.charmap_index().charmap(blob.data())?;
            if ['あ', 'ア', '曲'].iter().any(|ch| map.map(*ch).is_none_or(|g| g == 0)) {
                return None;
            }
            let data = FontData::from_blob(blob.into_raw_parts().0, f.index());
            Some(if f.has_weight_axis() { data.tweak(tweak(w.value(), None)) } else { data })
        };
        if let (Some(r), Some(b)) = (face(fontique::FontWeight::NORMAL), face(fontique::FontWeight::BOLD)) {
            return Some((r, b, name));
        }
    }
    None
}

/// Install the fonts and text sizes; returns the family actually used.
pub fn install_fonts(ctx: &egui::Context, family: &str) -> String {
    let (regular, bold_data, used) = font_pair(family);
    let mut defs = FontDefinitions::default();
    defs.font_data.insert("rhr-regular".into(), Arc::new(regular));
    defs.font_data.insert(BOLD.into(), Arc::new(bold_data));
    let fallbacks = defs.families.get(&FontFamily::Proportional).cloned().unwrap_or_default();
    defs.families.entry(FontFamily::Proportional).or_default().insert(0, "rhr-regular".into());
    let mut heading = vec![BOLD.to_string()];
    heading.extend(fallbacks);
    defs.families.insert(bold(), heading);
    // Japanese after everything else, so Latin text keeps its fonts
    if let Some((ja_regular, ja_bold, _)) = japanese_fonts() {
        defs.font_data.insert("rhr-ja".into(), Arc::new(ja_regular));
        defs.font_data.insert("rhr-ja-bold".into(), Arc::new(ja_bold));
        for fam in [FontFamily::Proportional, FontFamily::Monospace] {
            defs.families.entry(fam).or_default().push("rhr-ja".into());
        }
        defs.families.entry(bold()).or_default().push("rhr-ja-bold".into());
    }
    ctx.set_fonts(defs);
    ctx.all_styles_mut(|style| {
        style.text_styles = [
            (TextStyle::Small, FontId::proportional(11.0)),
            (TextStyle::Body, FontId::proportional(13.0)),
            (TextStyle::Button, FontId::proportional(13.0)),
            (TextStyle::Heading, FontId::new(16.0, bold())),
            (TextStyle::Monospace, FontId::monospace(12.5)),
        ]
        .into();
    });
    used
}

/// The visuals of the chosen theme (2.0: built-in or the user's own; "Match
/// the system" uses Light and Dark as the system switches).
pub fn install_theme(ctx: &egui::Context, theme: &str, custom: &[realhires::themes::Theme]) {
    use realhires::themes;
    let (light, dark, pref) = if theme == themes::SYSTEM || themes::builtin(theme).is_none() && !custom.iter().any(|t| t.name == theme) {
        (
            palette_of(&themes::builtin("Light").unwrap()),
            palette_of(&themes::builtin("Dark").unwrap()),
            egui::ThemePreference::System,
        )
    } else {
        let p = palette_of(&themes::resolve(theme, false, custom));
        (p, p, if p.dark { egui::ThemePreference::Dark } else { egui::ThemePreference::Light })
    };
    *ACTIVE.write().unwrap() = Some((light, dark));
    for (slot_dark, p) in [(false, light), (true, dark)] {
        let dark = p.dark;
        let mut v = if dark { egui::Visuals::dark() } else { egui::Visuals::light() };
        v.panel_fill = p.bg;
        v.window_fill = p.bg;
        v.extreme_bg_color = if dark { mix(p.bg, Color32::BLACK, 0.25) } else { mix(p.bg, Color32::WHITE, 0.7) };
        v.faint_bg_color = mix(p.bg, p.text, 0.035);
        v.override_text_color = None;
        v.hyperlink_color = p.accent;
        v.selection.bg_fill = p.accent.gamma_multiply(if dark { 0.55 } else { 0.85 });
        v.selection.stroke.color = Color32::WHITE;
        v.window_corner_radius = egui::CornerRadius::same(12);
        v.menu_corner_radius = egui::CornerRadius::same(8);
        for w in [
            &mut v.widgets.noninteractive,
            &mut v.widgets.inactive,
            &mut v.widgets.hovered,
            &mut v.widgets.active,
            &mut v.widgets.open,
        ] {
            w.corner_radius = egui::CornerRadius::same(6);
        }
        v.widgets.noninteractive.bg_stroke.color = p.separator;
        // text fields and buttons get a hairline, so a field on the page background shows
        v.widgets.inactive.bg_stroke = Stroke::new(1.0, p.separator);
        v.widgets.inactive.weak_bg_fill = mix(p.bg, p.text, if dark { 0.14 } else { 0.08 });
        v.widgets.inactive.bg_fill = v.widgets.inactive.weak_bg_fill;
        v.widgets.hovered.weak_bg_fill = p.hover;
        v.widgets.hovered.bg_fill = p.hover;
        v.slider_trailing_fill = true;
        v.panel_fill = p.bg;
        v.window_fill = p.bg;
        v.widgets.noninteractive.fg_stroke.color = p.text;
        v.widgets.inactive.fg_stroke.color = p.text;
        ctx.set_visuals_of(if slot_dark { egui::Theme::Dark } else { egui::Theme::Light }, v);
    }
    ctx.set_theme(pref);
}
