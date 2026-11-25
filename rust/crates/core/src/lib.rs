//! Universal Music Player (formerly Real-Hi-Res): a music player that also
//! detects fake high-resolution audio files; this crate is its core.
//!
//! The Rust port of the Python package `realhires`.  It decodes audio with
//! FFmpeg and runs the same forensic tests: a spectral test for the brick-wall
//! low-pass left by upsampling or a lossy source, a bit-depth test for padded
//! samples, and a value-grid test for 16-bit masters given a gain change or
//! dither.  Verdicts, reasons, settings and saved results are shared with the
//! Python version.

// The rules compare floats the way the Python version does, NaN included:
// `!(a <= b)` is true for NaN on purpose.
#![allow(clippy::neg_cmp_op_on_partial_ord, clippy::type_complexity, clippy::needless_range_loop)]

/// The product name (2.0; the quality checker keeps the name Real-Hi-Res).
pub const APP_NAME: &str = "Universal Music Player";
pub const PROJECT_URL: &str = "https://github.com/SU2H1/Universal-Music-Player";

pub mod analysis;
pub mod bitperfect;
pub mod covers;
pub mod dsp;
pub mod embed;
pub mod eq;
pub mod eta;
pub mod fix;
pub mod i18n;
pub mod imaging;
pub mod json;
pub mod library;
pub mod lyrics;
pub mod moods;
pub mod musicbrainz;
pub mod musiclib;
pub mod paths;
pub mod places;
pub mod playerstate;
pub mod playqueue;
pub mod probe;
pub mod pyfmt;
pub mod pyunicode;
pub mod report;
pub mod settings;
pub mod smarttabs;
pub mod stats;
pub mod store;
pub mod tagedit;
pub mod themes;
pub mod verdict;

/// The version of Real-Hi-Res this port corresponds to (also written into
/// saved results).  Kept equal to `realhires.__version__`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Number of jobs to run: 0 = one per CPU core, otherwise clamped to 1..512.
pub fn effective_jobs(jobs: i64) -> usize {
    if jobs <= 0 {
        std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
    } else {
        (jobs as usize).clamp(1, 512)
    }
}
