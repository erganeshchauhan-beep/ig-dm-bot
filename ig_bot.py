import csv
import logging
#import os
#import random
import sys
import time
from pathlib import Path
from typing import Literal, Optional, Tuple

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# 1. SINGLE-VARIABLE MODE SWITCH
SOURCE: Literal["csv", "llm"] = "llm"  # <-- Change to "csv" or "llm"

# 2. CONFIGURATION
POLL_INTERVAL_SEC: float = 3.0
CSV_BURST_DELAY_SEC: float = 0.8  # Delay between messages in CSV mode
CSV_FILE_PATH: Path = Path("yes_words.csv")
CHROME_PROFILE_DIR: Path = Path("./ig_chrome_profile").resolve()

LM_STUDIO_URL: str = "http://localhost:1234/v1/chat/completions"
LM_STUDIO_MODEL: str = "l3-8b-stheno-v3.2"  # <-- FIXED: exact model ID from your log
SYSTEM_PROMPT: str = (
    "You are a charming, witty guy texting a close friend (a girl) on Instagram DM. "
    "Reply with a short, playfully flirty line in natural Hinglish (Hindi written in English script). "
    "Keep it to one sentence, under 15 words. "
    "Sound like a real person from a metro city—use casual spelling, lowercase, and maybe one emoji. "
    "Be a bit teasing, a bit sincere. For example: 'tumhari smile toh insta feed se bhi zyada bright hai yaar'. "
    "Never use roleplay asterisks, formal 'aap', or pure English. No cringe pickup lines."
)

# Configure structured console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("IGBot")


# 3. BROWSER & DOM UTILITIES
def init_Browser() -> webdriver.Chrome:
    """Launches Chrome with a persistent session so login survives across runs."""
    options = Options()
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )
    return driver


def get_latest_message(driver: webdriver.Chrome) -> Tuple[Optional[str], bool]:
    """
    Robust newest-bubble reader.
    """
    js_script = """
    // ---- 1. Find the real message composer ----
    const composerCandidates = Array.from(
        document.querySelectorAll('div[role="textbox"][contenteditable="true"]')
    );
    const textbox = composerCandidates
        .filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 50 && r.height > 10 && r.top > window.innerHeight * 0.4;
        })
        .pop();
    if (!textbox) return { error: "NO_COMPOSER_TEXTBOX", totalTextBoxes: composerCandidates.length };
    const tbRect = textbox.getBoundingClientRect();

    // ---- 2. Find the chat scroll container (sibling, not ancestor) ----
    function findChatScroll(textbox) {
        const tbRect = textbox.getBoundingClientRect();

        // Strategy A: walk up the ancestor chain, search descendants for a scrollable box above
        let cur = textbox.parentElement;
        let depth = 0;
        while (cur && cur !== document.body && depth < 12) {
            const descendants = cur.querySelectorAll('div, section, main, ul, article');
            for (const el of descendants) {
                if (el === textbox || el.contains(textbox)) continue;
                const s = getComputedStyle(el);
                if (s.overflowY === 'visible') continue;
                const scrollable =
                    el.scrollHeight > el.clientHeight + 5 ||
                    (el.querySelectorAll('div[dir="auto"], span[dir="auto"]').length >= 2
                     && el.clientHeight > 150);
                if (!scrollable) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 150 || r.height < 100) continue;
                if (r.bottom > tbRect.top + 60) continue;   // must be above composer
                return { el: el, method: 'ancestor-scan@' + depth };
            }
            cur = cur.parentElement;
            depth++;
        }

        // Strategy B: page-wide geometric fallback
        const pageWide = document.querySelectorAll('div, section, main');
        let best = null, bestScore = -Infinity;
        for (const el of pageWide) {
            const s = getComputedStyle(el);
            if (s.overflowY === 'visible') continue;
            if (el.scrollHeight <= el.clientHeight + 5) continue;
            const r = el.getBoundingClientRect();
            if (r.width < 150 || r.height < 100) continue;
            if (r.bottom > tbRect.top + 100) continue;
            if (r.right < tbRect.left - 100 || r.left > tbRect.right + 100) continue;
            const score = -Math.abs(r.width - tbRect.width);
            if (score > bestScore) { bestScore = score; best = el; }
        }
        if (best) return { el: best, method: 'page-wide-fallback' };

        return null;
    }

    const found = findChatScroll(textbox);
    if (!found) return { error: "NO_SCROLL_PARENT", totalTextBoxes: composerCandidates.length };
    const scroll = found.el;
    const scrollMethod = found.method;
    const scrRect = scroll.getBoundingClientRect();
    const paneCenterX = scrRect.left + scrRect.width / 2;

    // ---- 3. Collect candidate text nodes inside the scroll container ----
    const candidates = Array.from(
        scroll.querySelectorAll('div[dir="auto"], span[dir="auto"]')
    );
    const ignoreSet = new Set([
        "message...", "send", "seen", "sent", "active now",
        "typing...", "like", "reply", "react",
    ]);
    const tsRe = /^(today|yesterday|mon|tue|wed|thu|fri|sat|sun|\\d{1,2}:\\d{2})/i;

    let bubbles = [];
    const dbg = { cands: candidates.length, wrapper: 0, size: 0, out: 0,
                  empty: 0, ignore: 0, ts: 0, kept: 0 };

    for (const node of candidates) {
        // Skip wrappers that contain an inner dir="auto" leaf
        if (node.querySelector('div[dir="auto"], span[dir="auto"]')) { dbg.wrapper++; continue; }
        const rect = node.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 10)          { dbg.size++;    continue; }
        // Must intersect the chat viewport
        if (rect.bottom < scrRect.top + 2)               { dbg.out++;     continue; }
        if (rect.top    > scrRect.bottom - 2)            { dbg.out++;     continue; }
        // Must be inside chat pane horizontally (5px tolerance)
        if (rect.right  < scrRect.left - 5)              { dbg.out++;     continue; }
        if (rect.left   > scrRect.right + 5)             { dbg.out++;     continue; }

        const text = (node.innerText || "").trim();
        if (!text)                                        { dbg.empty++;   continue; }
        const lower = text.toLowerCase();
        if (ignoreSet.has(lower))                         { dbg.ignore++;  continue; }
        if (lower.startsWith("seen ") ||
            lower.startsWith("active ") ||
            lower.startsWith("replied to "))              { dbg.ignore++;  continue; }
        if (tsRe.test(text) && text.length < 22)          { dbg.ts++;      continue; }

        bubbles.push({
            text: text,
            bottom: rect.bottom,
            left: rect.left,
            right: rect.right,
            centerX: rect.left + rect.width / 2,
        });
        dbg.kept++;
    }

    if (bubbles.length === 0) return { error: "NO_BUBBLES_FOUND", debug: dbg };

    bubbles.sort((a, b) => a.bottom - b.bottom);
    const newest = bubbles[bubbles.length - 1];

    // ---- 4. Sender detection ----
    const leftGap  = newest.left  - scrRect.left;
    const rightGap = scrRect.right - newest.right;
    const isFromFriend = leftGap < rightGap;   // left-aligned = theirs

    return {
        text: newest.text,
        isFromFriend: isFromFriend,
        debug: {
            ...dbg,
            method: scrollMethod,
            centerX:     Math.round(newest.centerX),
            paneCenterX: Math.round(paneCenterX),
            leftGap:     Math.round(leftGap),
            rightGap:    Math.round(rightGap),
            scrRect: [Math.round(scrRect.left), Math.round(scrRect.top),
                      Math.round(scrRect.right), Math.round(scrRect.bottom)],
        },
    };
    """
    try:
        result = driver.execute_script(js_script)
    except WebDriverException as exc:
        logger.warning(f"[read] WebDriverException: {exc}")
        return None, False

    if not result:
        logger.warning("[read] JS returned empty/null")
        return None, False
    if "error" in result:
        logger.warning(f"[read] {result['error']}  debug={result.get('debug')}")
        return None, False

    text = result.get("text")
    is_friend = bool(result.get("isFromFriend"))
    logger.debug(
        f"[read] {'FRIEND' if is_friend else 'US'} {text!r}  dbg={result.get('debug')}"
    )
    return text, is_friend


def get_composer(driver: webdriver.Chrome):
    """Helper to reliably find the bottom-most contenteditable textbox."""
    return driver.execute_script("""
        const candidates = Array.from(
            document.querySelectorAll('div[role="textbox"][contenteditable="true"]')
        );
        const real = candidates.filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 50 && r.height > 10 && r.top > window.innerHeight * 0.4;
        });
        return real.length ? real[real.length - 1] : null;
    """)


def send_dm_message(driver: webdriver.Chrome, text: str) -> bool:
    """
    Sends `text` through the DM composer and confirms it left the box.
    """
    try:
        # FIXED: Use the custom helper to ensure we grab the chat box, not the search bar
        textbox = WebDriverWait(driver, 5).until(
            lambda d: get_composer(d) or False
        )

        # Clear the box (safe if empty)
        driver.execute_script("""
            const el = arguments[0];
            el.focus();
            const sel = window.getSelection();
            sel.removeAllRanges();
            const r = document.createRange();
            r.selectNodeContents(el);
            sel.addRange(r);
            document.execCommand('delete');
        """, textbox)
        time.sleep(0.05)

        # Insert (returns whether execCommand succeeded)
        inserted = driver.execute_script("""
            const el = arguments[0], txt = arguments[1];
            el.focus();
            try { return document.execCommand('insertText', false, txt); }
            catch (e) { return false; }
        """, textbox, text)

        if not inserted:
            # Fallback: put cursor at end and type via WebDriver
            driver.execute_script("""
                const el = arguments[0];
                el.focus();
                const sel = window.getSelection();
                sel.selectAllChildren(el);
                sel.collapseToEnd();
            """, textbox)
            textbox.send_keys(text)

        time.sleep(0.15)
        typed = driver.execute_script("return arguments[0].innerText;", textbox) or ""
        if not typed.strip():
            logger.warning("[send] composer empty after insert — abort")
            return False

        # Send
        driver.execute_script("arguments[0].focus();", textbox)
        time.sleep(0.05)
        ActionChains(driver).send_keys(Keys.ENTER).perform()
        time.sleep(0.35)

        remaining = driver.execute_script("return arguments[0].innerText;", textbox) or ""
        if remaining.strip():
            # One retry using element.send_keys
            textbox.send_keys(Keys.ENTER)
            time.sleep(0.3)
            remaining = driver.execute_script("return arguments[0].innerText;", textbox) or ""
            if remaining.strip():
                logger.warning(f"[send] composer still contains {remaining!r} after Enter")
                return False
        return True

    except (NoSuchElementException, StaleElementReferenceException, WebDriverException) as exc:
        logger.warning(f"[send] failed: {exc}")
        return False


# 4. ENGINE A: CSV SPAM MODE
def run_csv_mode(driver: webdriver.Chrome) -> int:
    if not CSV_FILE_PATH.exists():
        default_words = ["yes", "yesss", "yup", "absolutely", "100%", "definitely", "yeah"]
        with CSV_FILE_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for word in default_words:
                writer.writerow([word])
        logger.info(f"Created sample {CSV_FILE_PATH} with {len(default_words)} words.")

    with CSV_FILE_PATH.open("r", encoding="utf-8-sig") as f:
        words = [
            cell.strip()
            for row in csv.reader(f)
            for cell in row
            if cell.strip()
        ]

    if not words:
        raise ValueError(f"{CSV_FILE_PATH} is empty. Add words before running CSV mode.")

    logger.info(f"CSV Mode active | Loaded {len(words)} words from {CSV_FILE_PATH}")
    sent_count = 0
    word_index = 0

    while True:
        _latest_msg, _is_friend = get_latest_message(driver)
        word = words[word_index % len(words)]
        word_index += 1

        if send_dm_message(driver, word):
            sent_count += 1
            logger.info(f"[CSV #{sent_count}] Sent: {word}")

        time.sleep(CSV_BURST_DELAY_SEC)


# 5. ENGINE B: LM STUDIO FLIRTY AUTO-REPLY MODE
def get_chat_history(driver: webdriver.Chrome, limit: int = 10) -> list[dict[str, str]]:
    """Scrapes the currently visible chat bubbles to build initial context."""
    js_script = """
    const composerCandidates = Array.from(
        document.querySelectorAll('div[role="textbox"][contenteditable="true"]')
    );
    const textbox = composerCandidates
        .filter(el => {
            const r = el.getBoundingClientRect();
            return r.width > 50 && r.height > 10 && r.top > window.innerHeight * 0.4;
        })
        .pop();
    if (!textbox) return [];

    function findChatScroll(textbox) {
        const tbRect = textbox.getBoundingClientRect();
        let cur = textbox.parentElement;
        let depth = 0;
        while (cur && cur !== document.body && depth < 12) {
            const descendants = cur.querySelectorAll('div, section, main, ul, article');
            for (const el of descendants) {
                if (el === textbox || el.contains(textbox)) continue;
                const s = getComputedStyle(el);
                if (s.overflowY === 'visible') continue;
                const scrollable = el.scrollHeight > el.clientHeight + 5 ||
                    (el.querySelectorAll('div[dir="auto"], span[dir="auto"]').length >= 2 && el.clientHeight > 150);
                if (!scrollable) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 150 || r.height < 100) continue;
                if (r.bottom > tbRect.top + 60) continue;
                return el;
            }
            cur = cur.parentElement;
            depth++;
        }
        return null;
    }

    const scroll = findChatScroll(textbox);
    if (!scroll) return [];
    const scrRect = scroll.getBoundingClientRect();
    const candidates = Array.from(scroll.querySelectorAll('div[dir="auto"], span[dir="auto"]'));
    const ignoreSet = new Set(["message...", "send", "seen", "sent", "active now", "typing...", "like", "reply", "react"]);
    const tsRe = /^(today|yesterday|mon|tue|wed|thu|fri|sat|sun|\\d{1,2}:\\d{2})/i;

    let bubbles = [];
    for (const node of candidates) {
        if (node.querySelector('div[dir="auto"], span[dir="auto"]')) continue;
        const rect = node.getBoundingClientRect();
        if (rect.width < 5 || rect.height < 10) continue;
        if (rect.bottom < scrRect.top + 2 || rect.top > scrRect.bottom - 2) continue;
        if (rect.right < scrRect.left - 5 || rect.left > scrRect.right + 5) continue;
        const text = (node.innerText || "").trim();
        if (!text) continue;
        const lower = text.toLowerCase();
        if (ignoreSet.has(lower) || lower.startsWith("seen ") || lower.startsWith("active ")) continue;
        if (tsRe.test(text) && text.length < 22) continue;

        bubbles.push({
            text: text,
            bottom: rect.bottom,
            left: rect.left,
            right: rect.right
        });
    }

    // Sort oldest to newest (top to bottom)
    bubbles.sort((a, b) => a.bottom - b.bottom);

    // Determine sender for each
    return bubbles.map(b => {
        const leftGap = b.left - scrRect.left;
        const rightGap = scrRect.right - b.right;
        return {
            text: b.text,
            isFromFriend: leftGap < rightGap
        };
    });
    """
    
    try:
        raw_bubbles = driver.execute_script(js_script)
        if not raw_bubbles:
            return []
            
        # Slice to the last `limit` messages so we don't overload the context
        raw_bubbles = raw_bubbles[-limit:]
        
        history = []
        for b in raw_bubbles:
            role = "user" if b["isFromFriend"] else "assistant"
            history.append({"role": role, "content": b["text"]})
            
        return history
    except WebDriverException as exc:
        logger.warning(f"[history] Failed to scrape initial history: {exc}")
        return []

def generate_flirty_reply_with_context(incoming_text: str, conversation_history: list, max_history: int) -> Optional[str]:
    """Queries LM Studio with a sliding window of past chat turns."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # Slice only the last N turns specified by the user
    if max_history > 0:
        messages.extend(conversation_history[-(max_history * 2):]) 
        
    messages.append({"role": "user", "content": incoming_text})

    payload = {
        "model": LM_STUDIO_MODEL,
        "messages": messages,
        "temperature": 0.85,
        "max_tokens": 60,
        "top_p": 0.9,
        "stop": ["\n", "User:", "Assistant:"],
    }

    try:
        response = requests.post(LM_STUDIO_URL, json=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        reply = data["choices"][0]["message"]["content"].strip().strip('"').strip("'")
        return reply
    except requests.RequestException as exc:
        logger.error(f"LM Studio connection error: {exc}")
        return None


def _check_lm_studio() -> None:
    """Verifies LM Studio is reachable and a model is actually loaded."""
    base = LM_STUDIO_URL.rsplit("/v1/", 1)[0]
    try:
        r = requests.get(f"{base}/v1/models", timeout=3)
        r.raise_for_status()
        models = [m.get("id") for m in r.json().get("data", [])]
        if not models:
            logger.error("LM Studio reachable but NO MODELS LOADED.")
        else:
            logger.info(f"LM Studio OK | loaded models: {models}")
            if LM_STUDIO_MODEL not in models:
                logger.warning(f"Configured model {LM_STUDIO_MODEL!r} is not in the loaded list.")
    except requests.RequestException as exc:
        logger.error(f"Cannot reach LM Studio at {base}: {exc}")


def run_llm_mode(driver: webdriver.Chrome) -> int:
    logger.info(f"LLM Mode | {LM_STUDIO_URL} | model={LM_STUDIO_MODEL} | poll={POLL_INTERVAL_SEC}s")
    _check_lm_studio()

    # Ask user how many past messages to keep in context
    try:
        user_input = input(">>> Enter how many past turns to use for context (e.g., 5 or 6): ").strip()
        max_history = int(user_input) if user_input else 6
    except ValueError:
        max_history = 6
        
    logger.info(f"Context memory set to last {max_history} turns.")

    # --- NEW: Load existing messages from the screen into memory ---
    logger.info("Reading current chat history from the screen...")
    conversation_history = get_chat_history(driver, limit=max_history * 2)
    if conversation_history:
        logger.info(f"Loaded {len(conversation_history)} previous messages into context.")
    else:
        logger.info("No previous messages found (or chat is empty). Starting fresh.")
    # ---------------------------------------------------------------

    sent_count = 0
    last_seen = None
    state = "IDLE"
    pending_friend_text: Optional[str] = None
    sent_at: Optional[float] = None
    read_fail_streak = 0

    while True:
        latest_text, is_from_friend = get_latest_message(driver)

        if latest_text is not None and (latest_text, is_from_friend) != last_seen:
            tag = "FRIEND" if is_from_friend else "US"
            logger.info(f"[bottom] {tag}: {latest_text!r}  (state={state})")
            last_seen = (latest_text, is_from_friend)

        if latest_text is None:
            read_fail_streak += 1
            if read_fail_streak in (5, 30, 100):
                logger.warning(f"[read] {read_fail_streak} consecutive failures — check DM thread.")
        else:
            read_fail_streak = 0

        # ---- state machine ----
        if state == "IDLE":
            if latest_text and is_from_friend:
                pending_friend_text = latest_text
                state = "PROCESSING"
                logger.info(f"[Friend] {latest_text}")

        elif state == "PROCESSING":
            reply = generate_flirty_reply_with_context(pending_friend_text, conversation_history, max_history)
            
            if not reply:
                logger.warning("[LLM] empty reply, retrying next cycle")
                time.sleep(1.0)
            elif send_dm_message(driver, reply):
                sent_count += 1
                
                conversation_history.append({"role": "user", "content": pending_friend_text})
                conversation_history.append({"role": "assistant", "content": reply})
                
                logger.info(f"[LLM #{sent_count}] {pending_friend_text!r} -> {reply!r}")
                sent_at = time.time()
                state = "SENT"
            else:
                logger.warning("[send] failed, retrying next cycle")
                time.sleep(1.0)

        elif state == "SENT":
            if latest_text and not is_from_friend:
                state = "IDLE"
                pending_friend_text = None
                sent_at = None
            elif sent_at and (time.time() - sent_at) > 10:
                logger.warning("[state] timed out waiting for own bubble — forcing IDLE")
                state = "IDLE"
                pending_friend_text = None
                sent_at = None

        time.sleep(POLL_INTERVAL_SEC)

# 6. MAIN ENTRYPOINT & STRICT DISPATCHER
def main() -> None:
    if SOURCE not in ("csv", "llm"):
        logger.critical("Invalid SOURCE value! Must be exactly 'csv' or 'llm'.")
        sys.exit(1)

    logger.info(f"Starting Instagram DM Bot in [{SOURCE.upper()}] mode...")
    driver = init_Browser()
    sent_count = 0

    try:
        driver.get("https://www.instagram.com/direct/inbox/")
        print("\n" + "=" * 62)
        print("ACTION REQUIRED:")
        print("1. Log in to Instagram in the Chrome window (saved for future runs).")
        print("2. Click into the target friend's DM thread so the chat is open.")
        print("=" * 62)
        input(">>> Press ENTER here once the DM chat is open on screen... ")

        if SOURCE == "csv":
            sent_count = run_csv_mode(driver)
        elif SOURCE == "llm":
            sent_count = run_llm_mode(driver)

    except KeyboardInterrupt:
        print()
        logger.info("=" * 50)
        logger.info(f"Bot stopped via Ctrl+C | Mode: {SOURCE.upper()}")
        logger.info(f"Total messages sent this session: {sent_count}")
        logger.info("=" * 50)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()