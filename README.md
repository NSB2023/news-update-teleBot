# MorningBird

Personal RSS news briefing: RSS content and excerpts → local TurboFieldfare Gemma → local Pocket TTS → one Telegram WAV file.

Edit `prompt.py` to change Gemma's briefing instructions. `main.py` imports its `build_news_prompt()` function when it analyzes each topic.

## Setup

1. Start your new Telegram bot and send it a message.
2. Copy `.env.example` to `.env`. Paste the real bot token there. Never share the token or commit `.env`.
3. Run `.venv/bin/python main.py chat-id`, then put the printed number into `TELEGRAM_CHAT_ID` in `.env`.
4. Start `TurboFieldfareServer` as described in its `docs/OPENAI_SERVER.md`. Keep it running. Do not run the TurboFieldfare Mac app at the same time.
5. From this folder run `.venv/bin/python main.py run`. Check Telegram for `daily_briefing.wav` and `briefing.md`.

## Sources and voice

Run `.venv/bin/python main.py sources` to print every active RSS URL. `config.json` controls them. Current publishers are Al Jazeera, Bangladesh's The Daily Star, BBC News, and The Guardian across world, business, technology, education, and politics. Selection takes one recent story from each available publisher before filling remaining slots, up to four stories per topic. The Daily Star's general `rss.xml` is stale, so this project uses its current category feeds. Al Jazeera's general feed is filtered by topic and excludes opinion, sport, and video items. To add another publisher, add its RSS URL and source name to the appropriate list in `config.json`. A feed that fails is skipped and logged.

Run `.venv/bin/python main.py voice-preview` to create samples for `alba`, `marius`, and `caro_davy` in `output/voice-previews/`. Listen with `afplay output/voice-previews/caro_davy.wav`. Set the preferred name in `config.json`. The current voice is `caro_davy`; voice quality is subjective. For your own voice, set `"voice": "voices/my_voice.wav"` after recording a clear sample and confirming cloning access.

For a same-day test after changing the feeds, prompt, or voice, run `.venv/bin/python main.py run --refresh`. It regenerates the audio and sends the changed files once.

The briefing collects up to 8 recent candidates from each feed, then asks Gemma to select up to 6 important, distinct events per topic. The selection rules prioritize public consequence and reject routine products, entertainment, promotional material, weak topic matches, and duplicate coverage. Publisher diversity is a secondary consideration. The selection and its reasons are saved in `output/YYYY-MM-DD/selection.json`. MorningBird then retrieves readable article text when available and falls back to a feed excerpt if the page cannot be read. Gemma writes one detailed point per selected story using only the available evidence. The five sections are combined into one recording; the actual duration is printed after generation and may exceed the earlier 5–7 minute target. The narration text is saved in `output/YYYY-MM-DD/narration.txt`.

The news lookback window is `hours_back` in `config.json` (currently 36 hours). `main.py` uses it to compute `cutoff` and excludes older dated feed items. The daily job starts at 06:15 in `schedule/com.morningbird.daily.plist`; `daily.py` waits until 08:00 Asia/Dhaka to send the audio. The earlier start gives six-story topic analysis enough time. Set your Mac's timezone to Asia/Dhaka for the 06:15 launch time.

Gemma selections and analyses are checkpointed per topic in `data/selection-cache/` and `data/analysis-cache/`. If a long run fails, repeat the same command; unchanged completed work is reused. Changes to the model, editorial prompt, candidates, selected stories, or article text automatically invalidate the relevant checkpoint.

The server path is `/Users/nazmussakib/Downloads/Work/Gemma_AI_Turbo/turbo-fieldfare`. Start its server with:

```sh
cd /Users/nazmussakib/Downloads/Work/Gemma_AI_Turbo/turbo-fieldfare
pgrep -fl 'TurboFieldfareServer|TurboFieldfareMac|TurboFieldfareDecodeService|TurboFieldfareCLI|TurboFieldfarePackageTests|swiftpm-testing-helper|mlx_lm|mlx-lm'
# Only if the previous command printed no match:
.build/release/TurboFieldfareServer --model scratch/gemma4.gturbo --port 8080 --max-context 16384
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

After a successful manual run, install the single macOS `launchd` job. At 06:15 it starts Gemma if needed, prepares the files, waits until 08:00, sends them, and stops only the Gemma server it started. Set the Mac's system timezone to Asia/Dhaka. The Mac must be awake and logged in at 06:15. `caffeinate` keeps it awake *while the job runs*; it cannot wake a sleeping Mac or a closed MacBook lid.

```sh
mkdir -p data "$HOME/Library/LaunchAgents"
cp schedule/com.morningbird.daily.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.morningbird.daily.plist"
```

Check `data/daily.log` and `data/daily-error.log` if delivery fails. To retry an incomplete run while Gemma is running: `.venv/bin/python main.py prepare` then `.venv/bin/python main.py send`. Successfully sent files are recorded in SQLite and skipped.

Article pages and RSS items vary: when an article page is unavailable, Gemma sees only the feed excerpt and is told to avoid inventing missing details. A live end-to-end run is still needed after changing the prompt and story count.
