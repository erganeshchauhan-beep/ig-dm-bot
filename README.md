# IGBot — Instagram DM Auto-Reply Bot

A Python bot that drives Instagram Web (`instagram.com/direct`) through Selenium and replies inside a single open DM thread. It has two engines, selected with one variable:

| Mode | `SOURCE` | What it does |
|------|----------|--------------|
| **LLM** | `"llm"` | Reads the newest message in the open chat, sends the recent conversation to a **local LLM served by LM Studio**, and posts the generated reply (short, playful Hinglish by default). |
| **CSV** | `"csv"` | Sends words from `yes_words.csv` in a loop with a fixed delay. |

> **Read the [Disclaimer](#disclaimer) before running this.**

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [LM Studio setup](#lm-studio-setup)
- [Configuration](#configuration)
- [Usage](#usage)
- [LLM mode state machine](#llm-mode-state-machine)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Security notes](#security-notes)
- [Disclaimer](#disclaimer)

---

## How it works

1. **Browser launch** — `init_Browser()` starts Chrome with a persistent profile (`./ig_chrome_profile`), so you log in once and the session is reused on later runs. It also hides the `navigator.webdriver` flag and disables the automation infobar.
2. **Manual handoff** — the script opens the Instagram inbox and waits. You log in, open the target DM thread, and press ENTER in the terminal.
3. **DOM reading** — Instagram's class names are obfuscated and change often, so the bot does not use them. Instead, a JavaScript snippet:
   - finds the message composer (`div[role="textbox"][contenteditable="true"]` in the lower 60% of the viewport),
   - finds the scrollable chat container above the composer (ancestor scan, then a page-wide geometric fallback),
   - collects leaf `div/span[dir="auto"]` nodes inside it, filtering out UI text ("Seen", "Active now", timestamps, "Replied to …"),
   - picks the bottom-most bubble as the newest message,
   - decides the sender by geometry: a bubble closer to the left edge of the chat pane is from the friend, closer to the right edge is from you.
4. **Sending** — `send_dm_message()` clears the composer, inserts text with `document.execCommand('insertText')` (falls back to WebDriver `send_keys`), presses ENTER, and verifies the composer is empty afterwards (one retry).
5. **Generation (LLM mode)** — `generate_flirty_reply_with_context()` posts an OpenAI-compatible chat-completions request to LM Studio with the system prompt, a sliding window of recent turns, and the new message.

---

## Requirements

- Python **3.9+** (uses `list[dict[str, str]]` type hints)
- Google Chrome (current stable)
- Selenium **4.6+** (includes Selenium Manager, so no manual chromedriver download)
- [LM Studio](https://lmstudio.ai/) with the local server running (LLM mode only)
- An Instagram account that can use the web DM interface

`requirements.txt`:

```
selenium>=4.15
requests>=2.31
```

---

## Installation

```bash
git clone https://github.com/erganeshchauhan-beep/ig-dm-bot.git
cd ig-dm-bot

python -m venv .venv
# Windows (PowerShell): .venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

---

## LM Studio setup

1. Download a chat model in LM Studio. The default config expects `l3-8b-stheno-v3.2`.
2. Open the **Developer / Local Server** tab and load the model.
3. Start the server (default `http://localhost:1234`).
4. Confirm it responds:
   ```bash
   curl http://localhost:1234/v1/models
   ```
5. Copy the exact model ID from that output into `LM_STUDIO_MODEL`.

On startup the bot calls `/v1/models` and logs a warning if the configured model is not in the loaded list.

---

## Configuration

All settings are constants at the top of the script.

| Variable | Default | Description |
|----------|---------|-------------|
| `SOURCE` | `"llm"` | `"llm"` or `"csv"`. Anything else exits with a critical error. |
| `POLL_INTERVAL_SEC` | `3.0` | Seconds between DOM reads in LLM mode. |
| `CSV_BURST_DELAY_SEC` | `0.8` | Delay between messages in CSV mode. |
| `CSV_FILE_PATH` | `yes_words.csv` | Word list for CSV mode. Auto-created with sample words if missing. |
| `CHROME_PROFILE_DIR` | `./ig_chrome_profile` | Persistent Chrome profile (holds your login session). |
| `LM_STUDIO_URL` | `http://localhost:1234/v1/chat/completions` | LM Studio chat endpoint. |
| `LM_STUDIO_MODEL` | `l3-8b-stheno-v3.2` | Must match a loaded model ID. |
| `SYSTEM_PROMPT` | Hinglish, playful, one sentence, under 15 words | Controls persona and style. |

LLM request parameters (in `generate_flirty_reply_with_context`): `temperature=0.85`, `top_p=0.9`, `max_tokens=60`, stop sequences `["\n", "User:", "Assistant:"]`, request timeout 15 s.

### CSV file format

One word or phrase per cell. Rows and cells are flattened into a single list, blank cells are skipped.

```csv
yes
yesss
absolutely
100%
```

---

## Usage

```bash
python ig_bot.py
```

1. Chrome opens the Instagram inbox.
2. Log in (first run only) and **open the one-to-one DM thread** you want the bot to work in.
3. Return to the terminal and press **ENTER**.
4. In LLM mode, enter how many past turns to include as context (default `6`).
5. Stop with **Ctrl+C**.

Example log output:

```
14:02:11 | INFO    | Starting Instagram DM Bot in [LLM] mode...
14:02:30 | INFO    | LM Studio OK | loaded models: ['l3-8b-stheno-v3.2']
14:02:34 | INFO    | Context memory set to last 6 turns.
14:02:34 | INFO    | Loaded 8 previous messages into context.
14:02:40 | INFO    | [Friend] kya kar rahi ho
14:02:42 | INFO    | [LLM #1] 'kya kar rahi ho' -> 'tumhare reply ka wait, aur kya 😄'
```

---

## LLM mode state machine

```
        friend bubble is newest
IDLE ───────────────────────────► PROCESSING
  ▲                                   │ generate reply + send
  │                                   ▼
  │   own bubble becomes newest     SENT
  └───────────────────────────────────┘
        (or 10 s timeout → forced IDLE)
```

- **IDLE** — wait until the newest bubble is from the friend.
- **PROCESSING** — call the LLM, send the reply; retry next cycle if either step fails.
- **SENT** — wait until your own bubble is the newest one, then return to IDLE. A 10-second timeout forces IDLE if the bot can't see its own message.

---

## Project structure

```
.
├── ig_bot.py            # entire bot
├── requirements.txt
├── LICENSE              # MIT
├── yes_words.csv        # CSV mode word list (auto-generated)
├── ig_chrome_profile/   # Chrome profile with your login session (DO NOT COMMIT)
└── README.md
```

Main functions:

| Function | Purpose |
|----------|---------|
| `init_Browser()` | Launch Chrome with persistent profile |
| `get_latest_message()` | Return `(text, is_from_friend)` for the newest bubble |
| `get_chat_history()` | Scrape visible bubbles into `user`/`assistant` messages |
| `get_composer()` | Locate the real message box (not the search box) |
| `send_dm_message()` | Insert text, press ENTER, verify it was sent |
| `generate_flirty_reply_with_context()` | Query LM Studio with sliding-window context |
| `_check_lm_studio()` | Startup health check |
| `run_csv_mode()` / `run_llm_mode()` | The two engines |

---

## Troubleshooting

The reader returns these error codes in the logs:

| Log message | Cause | Fix |
|-------------|-------|-----|
| `NO_COMPOSER_TEXTBOX` | No chat is open, or the window is too small so the composer is not in the lower 60% of the viewport. | Open a DM thread; keep the window maximized. |
| `NO_SCROLL_PARENT` | The message pane could not be located. | Instagram changed its DOM; adjust `findChatScroll`. |
| `NO_BUBBLES_FOUND` | Chat is empty or all bubbles were filtered. Check the `debug` counters in the log. | Send one message manually; review `ignoreSet`. |
| `Cannot reach LM Studio` | Server not running or wrong port. | Start the LM Studio server; check `LM_STUDIO_URL`. |
| `Configured model ... is not in the loaded list` | Model ID mismatch. | Copy the ID from `/v1/models`. |
| `[send] composer still contains ...` | ENTER did not submit. | Make sure the thread is focused and not showing a dialog. |
| Bot replies to itself | Sender detection misclassified your bubble. | Check `leftGap`/`rightGap` with `logging.DEBUG`. |

Enable detailed reader output with `level=logging.DEBUG` in `logging.basicConfig`.

---

## Known limitations

- Works on **one-to-one chats** only. In group chats, left/right alignment cannot identify the sender.
- Reads **text only**. Photos, voice notes, stickers, and reels are not understood.
- Depends on Instagram's current DOM structure; it may break without notice.
- Only the **newest** friend message is answered, even if several arrived in a row.
- Replies are sent immediately after generation, with no human-like delay.
- The Hinglish quality depends entirely on the model; small 8B models produce uneven results.

---

## Security notes

- `ig_chrome_profile/` contains your **session cookies**. Anyone with this folder can access your account. Add it to `.gitignore` and never push it.
- Suggested `.gitignore`:
  ```
  ig_chrome_profile/
  yes_words.csv
  __pycache__/
  .venv/
  .env
  ```
- The script opens Chrome remote debugging on port `9222` and passes `--no-sandbox`. Remove both unless you need them.

---

## Disclaimer

This project is for **personal learning and experimentation**.

- Automating Instagram's web interface violates Instagram's Terms of Use. Your account can be rate-limited, action-blocked, or banned. Use a throwaway account to test.
- CSV mode sends rapid repeated messages and will trigger spam protection quickly.
- Use it only in conversations where the other person is aware of and agrees to automated or AI-generated replies. Do not use it to spam, harass, or deceive anyone.
- The authors accept no responsibility for account actions or any other consequences.

---

## License

Released under the [MIT License](LICENSE).
