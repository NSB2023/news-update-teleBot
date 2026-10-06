# MorningBird

Personal RSS news briefing: RSS content and excerpts → local Ollama Qwen → local Pocket TTS → Telegram OGG/Opus voice messages.

## Configure in your browser

Run `.venv/bin/python ui_server.py` from this folder. MorningBird opens its local settings desk at `http://127.0.0.1:8765`. Choose your home country, regions, news sections, publishers, stories per section, start time, narrator, and installed Ollama model, then click **Save my edition**. The page writes these choices to `config.json`; it does not run or send a briefing. Close the terminal or press Ctrl-C when finished. The page is only served on your Mac's loopback address.

The page's large newspaper photograph is generated editorial artwork, not an image of a real current event. A small "From today's wires" strip shows current article images only when the configured publishers supply them in RSS; each image links to its report and names the publisher. Stories and source links in your actual briefing come from the configured RSS feeds. Country and region choices guide the model's story selection; they do not create new country-specific feeds. The configured `start_time` is read by the existing daily scheduler, which checks every 15 minutes.

Edit `prompt.py` to change Qwen's briefing instructions. `main.py` imports its `build_news_prompt()` function when it analyzes each topic.

## Setup

1. Start your new Telegram bot and send it a message.
2. Copy `.env.example` to `.env`. Paste the real bot token there. Never share the token or commit `.env`.
3. Run `.venv/bin/python main.py chat-id`, then put the printed number into `TELEGRAM_CHAT_ID` in `.env`.
4. Install Ollama and download the model with `ollama pull qwen3.5:4b`.
5. Install the Telegram voice encoder with `brew install ffmpeg`.
6. From this folder run `.venv/bin/python main.py run`. Check Telegram for one OGG/Opus voice message per completed topic plus `briefing.md`.

## Sources and voice

Run `.venv/bin/python main.py sources` to print every active RSS URL. `config.json` controls them. Current publishers are Al Jazeera, Bangladesh's The Daily Star, BBC News, and The Guardian across world, business, technology, education, and politics. Selection takes one recent story from each available publisher before filling remaining slots, up to four stories per topic. The Daily Star's general `rss.xml` is stale, so this project uses its current category feeds. Al Jazeera's general feed is filtered by topic and excludes opinion, sport, and video items. To add another publisher, add its RSS URL and source name to the appropriate list in `config.json`. A feed that fails is skipped and logged.

Run `.venv/bin/python main.py voice-preview` to create samples for `alba`, `marius`, and `caro_davy` in `output/voice-previews/`. Listen with `afplay output/voice-previews/caro_davy.wav`. Set the preferred name in `config.json`. The current voice is `caro_davy`; voice quality is subjective. For your own voice, set `"voice": "voices/my_voice.wav"` after recording a clear sample and confirming cloning access.

For a same-day test after changing the feeds, prompt, or voice, run `.venv/bin/python main.py run --refresh`. It regenerates the audio and sends changed files once. Use `--edition afternoon` to create a separate afternoon directory.

The briefing collects up to 8 recent candidates from each feed, then asks Qwen to select up to 6 important, distinct events per topic. The selection rules prioritize public consequence and reject routine products, entertainment, promotional material, weak topic matches, and duplicate coverage. Publisher diversity is a secondary consideration. The selection and its reasons are saved in `output/YYYY-MM-DD/selection.json`. MorningBird then retrieves readable article text when available and falls back to a feed excerpt if the page cannot be read. Qwen writes one detailed point per selected story using only the available evidence. The five sections are combined into one recording; the actual duration is printed after generation and may exceed the earlier 5–7 minute target. The narration text is saved in `output/YYYY-MM-DD/narration.txt`.

The news lookback window, story limits, publisher cap, evidence threshold, edition, TTS chunk size, and `start_time` are controlled by `config.json`. The LaunchAgent checks every 15 minutes; `daily.py` starts work only after the configured time and exits immediately after that edition has been delivered. Set the Mac's timezone to the configured timezone.

Qwen selections and analyses are checkpointed per topic in `data/selection-cache/` and `data/analysis-cache/`. Model requests are retried once. A failed topic does not stop other topics, and `errors.md` explains feed, model, extraction, TTS, or encoding failures. Changes to the model, editorial prompt, candidates, selected stories, or article text automatically invalidate the relevant checkpoint.

Each run uses this output structure:

```text
output/YYYY-MM-DD/morning/
├── YYYY-MM-DD_morning_world.md
├── YYYY-MM-DD_morning_world.txt
├── YYYY-MM-DD_morning_world.wav
├── YYYY-MM-DD_morning_world.ogg
├── briefing.md
├── selection.json
├── errors.md                 # only when something failed
└── manifest.json
```

Pocket TTS receives normalized sentence-sized chunks rather than one long script. Dates, common abbreviations, currencies, percentages, and numbers are made speech-friendly; short pauses are inserted between chunks. WAV files remain as recoverable local intermediates, while Telegram receives OGG/Opus voice messages.

SQLite stores candidate and selected articles, normalized titles, publishers, publication times, topics, evidence type, edition, summaries, run state, and delivery state. It also prevents duplicate Telegram deliveries.

Test the Ollama model with:

```sh
ollama list
ollama run qwen3.5:4b "Reply with: model ready"
```

In a second terminal:

```sh
cd /Users/nazmussakib/Downloads/news-update-teleBot
cp .env.example .env
# Edit .env in a text editor, then:
.venv/bin/python main.py chat-id
# Add the chat ID to .env, then:
.venv/bin/python main.py run
```

To use your own voice, save a clean recording as `voices/my_voice.wav` and change `"voice": "alba"` in `config.json` to `"voice": "voices/my_voice.wav"`. Voice cloning requires access to the cloning weights.

## Daily schedule

After a successful manual run, install the single macOS `launchd` job. It checks every 15 minutes and reads the actual briefing start time from `config.json`. It starts Ollama only for a due run and stops only the Ollama server it started. The Mac must be powered on and the user logged in. `caffeinate` keeps it awake while generation runs; it cannot operate while the Mac is powered off.

```sh
mkdir -p data "$HOME/Library/LaunchAgents"
cp schedule/com.morningbird.daily.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.morningbird.daily.plist"
```

Check `data/daily.log` and `data/daily-error.log` if delivery fails. To retry an incomplete run: `.venv/bin/python main.py prepare` then `.venv/bin/python main.py send`. Successfully sent files are recorded in SQLite and skipped.

Article pages and RSS items vary: when an article page is unavailable, Qwen sees only the feed excerpt and is told to avoid inventing missing details. A live end-to-end run is still needed after changing the prompt and story count.
