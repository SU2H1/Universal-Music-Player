# Japanese translation: glossary and style

The apps' texts are written in English in the code (`tr("…")` in Python, `tr!("…")` in Rust) and translated in
`i18n/ja/*.json` (one file per part of the apps); `python tests/i18n_keys.py --merge` joins them into
`realhires/assets/lang/ja.json`, which both apps read. `python tests/i18n_keys.py` lists the texts that have no
translation yet.

## Style

- Follow the wording of Apple Music in Japanese where it has a term, because the player is modelled on it.
- Buttons, menu entries, column headings and tab names are short noun or verb forms without 。 ("保存", "フォルダを開く",
  "ライブラリを今すぐ更新").
- Sentences (help texts, messages, notes) use です/ます and end with 。; Japanese punctuation 、。「」 and full-width
  ？！（）；： inside Japanese sentences. Keep "…" at the end of entries that open a window ("名前を変更…").
- Placeholders such as `{n}` must appear in the translation exactly as in the English text (same names, same count).
- Product and service names stay in Latin letters: Universal Music Player, Real-Hi-Res, MusicBrainz, LRCLIB, LRCGET,
  FFmpeg, FLAC, MP3, AAC, ALAC, WAV, Equalizer APO, AutoEQ, SF Pro, Inter. File names, file extensions (.lrc, .txt,
  .json), setting values in quotes that the user types, units (kHz, dB, kbps, MB) and keyboard keys stay as they are.
- Numbers: "{n} songs" → "{n}曲", "{n} minutes" → "{n}分", "{n} of {total}" → "{n}/{total}" or "{total}件中{n}件".

## Terms

| English | 日本語 |
| --- | --- |
| Library | ライブラリ |
| All Songs / Songs | すべての曲 / 曲 |
| Folders | フォルダ |
| Albums / Album | アルバム |
| Artists / Artist | アーティスト |
| Album Artists / Album artist | アルバムアーティスト |
| Genres / Genre | ジャンル |
| Years / Year | 年 |
| Composers / Composer | 作曲者 |
| Playlists / Playlist / New Playlist… | プレイリスト / 新規プレイリスト… |
| Liked Songs / Like / Unlike | お気に入りの曲 / お気に入り / お気に入りを解除 |
| Disliked Songs / Dislike / Remove dislike | 好きじゃない曲 / 好きじゃない / 「好きじゃない」を取り消す |
| Recently Played / Recently Added | 最近再生した項目 / 最近追加した項目 |
| Your Stats | あなたの統計 |
| Now Playing / Up Next / Playing from | 再生中 / 次に再生 / 再生元 |
| Lyrics / Synced lyrics / Plain lyrics | 歌詞 / 同期歌詞 / テキストの歌詞 |
| Play / Pause / Next / Previous / Shuffle / Repeat | 再生 / 一時停止 / 次へ / 前へ / シャッフル / リピート |
| Volume / Mute | 音量 / 消音 |
| Equalizer / Preset / Pre-amp / Band / Graphic / Parametric | イコライザ / プリセット / プリアンプ / バンド / グラフィック / パラメトリック |
| Playback info / Decoder / Output / Device | 再生情報 / デコーダ / 出力 / デバイス |
| Sample rate / Bit depth / Bit rate / Channels / Stereo / Mono | サンプリング周波数 / ビット深度 / ビットレート / チャンネル / ステレオ / モノラル |
| Gapless playback | ギャップレス再生 |
| Settings / Appearance / Theme / Font / Language / Playback | 設定 / 外観 / テーマ / フォント / 言語 / 再生 |
| Edit Info / Title / Release date / Track / Disc | 情報を編集 / タイトル / リリース日 / トラック / ディスク |
| Quality check / Scan / Re-scan | 品質チェック / スキャン / 再スキャン |
| Fix to real resolution / Fix | 実際の解像度に修正 / 修正 |
| hi-res / lossless / lossy | ハイレゾ / ロスレス / 非可逆圧縮（ロッシー） (short: ロッシー) |
| Fake lossless / Fake hi-res / Suspicious / Real hi-res / CD quality / Lossy format / Error | 偽ロスレス / 偽ハイレゾ / 疑わしい / 本物のハイレゾ / CD品質 / ロッシー形式 / エラー |
| FAKE LOSSLESS / FAKE HI-RES / SUSPICIOUS / REAL HI-RES / NOT HI-RES / LOSSY FORMAT / ERROR (verdict names) | 偽ロスレス / 偽ハイレゾ / 疑わしい / 本物のハイレゾ / ハイレゾではない / ロッシー形式 / エラー |
| Not scanned / Analysing… | 未スキャン / 解析中… |
| Saved results / Detection / Scanning / Display / Fixing | 保存された結果 / 検出 / スキャン / 表示 / 修正 |
| Spectrogram / Spectrum / Bandwidth / Upsampled / Padded | スペクトログラム / スペクトル / 帯域幅 / アップサンプリング / パディング |
| Source(s) to scan | スキャン対象 |
| Open folder / Open file / Copy path / Show in folder | フォルダを開く / ファイルを開く / パスをコピー / フォルダに表示 |
| Search / Find | 検索 / 検索 |
| Save / Cancel / Close / OK / Apply / Delete… / Rename… / Remove | 保存 / キャンセル / 閉じる / OK / 適用 / 削除… / 名前を変更… / 取り除く |
| Import… / Export… | 読み込む… / 書き出す… |
| Add folder… / Choose a music folder… | フォルダを追加… / 音楽フォルダを選択… |
| Update the library now | ライブラリを今すぐ更新 |
| Clear the play history… | 再生履歴を消去… |
| Unknown Artist / Unknown Album / Unknown Genre / Unknown Composer / Unknown Year | 不明なアーティスト / 不明なアルバム / 不明なジャンル / 不明な作曲者 / 不明な年 |
| Match the system | システムに合わせる |
| Get info from MusicBrainz… | MusicBrainzから情報を取得… |
