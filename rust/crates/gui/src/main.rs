//! Real-Hi-Res desktop app (Rust).  `realhires-gui [PATH...]`: paths given on
//! the command line are added and scanned at once.

#![cfg_attr(windows, windows_subsystem = "windows")]

mod app;
mod audio;
#[cfg(windows)]
mod audio_wasapi;
mod cover_cache;
mod coverwin;
mod dialogs;
mod editinfo;
mod eqwin;
mod icons;
mod labels;
mod look;
mod library_pages;
mod lyrics;
mod mbwin;
mod pages;
mod player;
mod plots;
mod style;
mod tabs;
mod upnext;
mod web;
mod work;

/// The headless app tests set process-wide variables (where results, settings,
/// the library and the player state live), so they run one at a time.
#[cfg(test)]
pub static TEST_ENV: std::sync::Mutex<()> = std::sync::Mutex::new(());

const ICON: &[u8] = include_bytes!("../../../../realhires/assets/icon.png");

struct GuiApp(player::Player);

impl eframe::App for GuiApp {
    fn ui(&mut self, ui: &mut egui::Ui, _frame: &mut eframe::Frame) {
        self.0.ui(ui);
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        self.0.on_exit();
    }
}

fn icon() -> Option<egui::IconData> {
    let img = image::load_from_memory(ICON).ok()?.to_rgba8();
    Some(egui::IconData { width: img.width(), height: img.height(), rgba: img.into_raw() })
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|a| a == "--version") {
        println!("universal-music-player {}", realhires::VERSION);
        return;
    }
    if args.iter().any(|a| a == "-h" || a == "--help") {
        println!("usage: universal-music-player [PATH...]\n\nUniversal Music Player: a music player with the Real-Hi-Res quality checker. Folders or files given here are opened in the Quality check page and scanned at once.");
        return;
    }
    let cfg = realhires::settings::load(None);
    // already here for the message below, in case the window cannot open
    realhires::i18n::set_language(&cfg.language);
    let mut viewport = egui::ViewportBuilder::default()
        .with_title(realhires::APP_NAME)
        .with_inner_size([1280.0, 840.0])
        .with_min_inner_size([980.0, 620.0])
        .with_drag_and_drop(true);
    if let Some(i) = icon() {
        viewport = viewport.with_icon(i);
    }
    let options = eframe::NativeOptions { viewport, ..Default::default() };
    let res = eframe::run_native(
        realhires::APP_NAME,
        options,
        Box::new(move |cc| Ok(Box::new(GuiApp(player::Player::new(&cc.egui_ctx, cfg, args))))),
    );
    if let Err(e) = res {
        let text = realhires::tr!(
            "{app} cannot open its window: {e}\n\nThe desktop app needs a graphical desktop with OpenGL 2.0 or \
             newer (on Linux X11 or Wayland); updating the graphics driver usually helps. The command-line tool \
             `realhires` works without one.",
            app = realhires::APP_NAME,
            e = e
        );
        eprintln!("{text}");
        // a windowed program has no console on Windows, so say it where it can be seen
        let _ = rfd::MessageDialog::new()
            .set_level(rfd::MessageLevel::Error)
            .set_title(realhires::APP_NAME)
            .set_description(&text)
            .set_buttons(rfd::MessageButtons::Ok)
            .show();
        std::process::exit(1);
    }
}
