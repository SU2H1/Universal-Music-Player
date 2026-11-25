//! The browser and the clipboard (2.3): Search Cover Online and the Properties
//! window's Search buttons open the system's browser (`open_url()`, the
//! `webbrowser` crate, as egui's own links do), and the Properties window then
//! watches the clipboard (`clipboard_text()`, the system clipboard through
//! `arboard`).  In the tests both are fakes for the calling thread (`fake`):
//! the addresses are recorded and the clipboard is a string the test sets, so a
//! test never opens a real browser or reads the real clipboard.

/// Open a web page in the system's browser; Err(what went wrong) when it could not.
pub fn open_url(url: &str) -> Result<(), String> {
    #[cfg(test)]
    {
        fake::OPENED.with(|o| o.borrow_mut().push(url.to_string()));
        Ok(())
    }
    #[cfg(not(test))]
    {
        webbrowser::open(url).map_err(|e| e.to_string())
    }
}

#[cfg(not(test))]
thread_local! {
    /// One connection to the system clipboard (made on the first look).
    static CLIPBOARD: std::cell::RefCell<Option<arboard::Clipboard>> = const { std::cell::RefCell::new(None) };
}

/// The text on the clipboard, or "" when it holds none (or cannot be read).
pub fn clipboard_text() -> String {
    #[cfg(test)]
    {
        fake::CLIPBOARD.with(|c| c.borrow().clone())
    }
    #[cfg(not(test))]
    {
        CLIPBOARD.with(|c| {
            let mut c = c.borrow_mut();
            if c.is_none() {
                *c = arboard::Clipboard::new().ok();
            }
            c.as_mut().and_then(|cb| cb.get_text().ok()).unwrap_or_default()
        })
    }
}

#[cfg(test)]
pub mod fake {
    use std::cell::RefCell;

    thread_local! {
        pub static OPENED: RefCell<Vec<String>> = const { RefCell::new(Vec::new()) };
        pub static CLIPBOARD: RefCell<String> = const { RefCell::new(String::new()) };
    }

    /// The addresses opened on this thread so far.
    pub fn opened() -> Vec<String> {
        OPENED.with(|o| o.borrow().clone())
    }

    /// What the clipboard holds from now on (a copy in the browser).
    pub fn copy(text: &str) {
        CLIPBOARD.with(|c| *c.borrow_mut() = text.to_string());
    }
}
