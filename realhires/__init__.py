"""Universal Music Player (formerly Real-Hi-Res): a music player that also detects
fake high-resolution and fake lossless audio files.

The quality checker (the "Real-Hi-Res" part) decodes audio with ffmpeg and runs
two independent forensic tests:

* a spectral test that looks for the brick-wall low-pass left behind when a
  44.1/48 kHz (or lossy) master is upsampled to 88.2/96/176.4/192 kHz, and
* a bit-depth test that measures how many bits of each sample are actually used,
  which exposes 16-bit audio padded with zeros into a 24-bit container.
"""

__version__ = "2.4.0"
APP_NAME = "Universal Music Player"
PROJECT_URL = "https://github.com/SU2H1/Universal-Music-Player"
