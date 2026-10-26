# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EasyT is a YouTube video subtitle translation tool that downloads videos/transcripts, applies LLM-based punctuation, translates content (from the video's own language into the host's, as its locale names it), and generates SRT subtitle files. It also extracts advanced terms, and takes plugins that add commands of their own.

## Common Commands

```bash
# Install dependencies (requires Python >=3.12,<3.13)
uv sync

# Translate a YouTube video or playlist, given as a full URL (quoted: ? & [ ] are shell syntax)
uv run easyt "https://www.youtube.com/watch?v=Qmafkf_08uQ"
uv run easyt "https://www.youtube.com/playlist?list=<id>"        # Full playlist
uv run easyt "https://www.youtube.com/playlist?list=<id>[0:5]"   # Playlist slice
uv run easyt <url> --resolution 720p           # Video quality: best, 1440p, 1080p (default), 720p
uv run easyt <url> -t ko                       # Target language: ko, pt-BR, zh-Hant, … (default: the locale's)

# Other modes
uv run easyt aterm <url>                       # Advanced terms extraction (HTML output)
uv run easyt transcript <url>                  # Transcript text only
uv run easyt auth                              # Sign in to Google Drive in the browser

# Telegram bot, as the systemd user service easyt-bot
uv run easyt install                           # Write its unit, enable it and (re)start it
uv run easyt start|stop|restart|status         # systemctl --user ... easyt-bot
uv run easyt log                               # journalctl --user -u easyt-bot -f
uv run easyt uninstall
uv run easyt bot                               # The bot in the foreground, refused while the service runs

# Run tests (uses pytest-asyncio with asyncio_mode = "auto"); tests marked `live` are skipped
uv run pytest
uv run pytest -m live                          # Only the tests that call real subscriptions or paid APIs
uv run pytest tests/test_bot.py -v             # Single test file
```

## Architecture

### Pipeline System

The application uses a pipeline architecture with a base class and specialized pipelines, all in [pipeline/](src/easyt/pipeline/):

- **Pipeline** ([standard.py](src/easyt/pipeline/standard.py)) - Standard video translation: punctuation → LLM translation (Claude, or GPT through Codex) → timestamp mapping → SRT generation
- **Pipeline_aterm** ([aterm.py](src/easyt/pipeline/aterm.py)) - Advanced terms mode: extracts vocabulary with definitions, generates HTML output
- **Pipeline_base** ([base.py](src/easyt/pipeline/base.py)) - Common functionality: punctuation, and sending a result to the Telegram chat through `send_document()` ([notify.py](src/easyt/notify.py))

All pipeline `run()` methods are async (entry point uses `asyncio.run()`).

### Entry Points

- **CLI** ([app.py](src/easyt/app.py)) - `easyt URL` translates; `easyt COMMAND` runs another mode, the bot, the Drive sign-in, or drives the bot's service. The YouTube modes go through `run_youtube()` ([jobs.py](src/easyt/jobs.py)): `parse_youtube_url()` reads the full video or playlist URL, the mode's pipeline runs on each video (translating into `-t`, or else `host_language()`, the locale's language as Babel reads it, which the bot always does), and the outputs are uploaded to Google Drive. A plugin's command runs its own `run()`
- **Telegram bot** ([bot.py](src/easyt/bot.py)) - Answers only `TELEGRAM_CHAT_ID`. A pasted URL is translated and answered with its Drive folder link; `/aterm` and `/transcript` have their pipelines send the HTML or TXT to the chat, and a plugin's `/NAME` queues its own `job()`. Jobs run one at a time as asyncio tasks in the bot's process, each with a message drawing the tree of its spans: a finished span a line, the running ones with how far they are (`14/47`, `64%`), the deepest with its last log lines; plus `/status`, `/cancel` and a Cancel button. A job running or waiting when the bot stops is dropped, not resumed
- **Plugins** ([plugins.py](src/easyt/plugins.py)) - A plugin is a package in `~/.easyt/plugins` (`PLUGINS_DIR`), often a link to where it is developed, whose `command.py` names `command`: a `Command` with the CLI's help, arguments and `run()`, and the bot's help line and `job()`, which a `ValueError` turns into the bot's answer. `commands()` imports them; app.py adds each as a subcommand, and bot.py as a /command in its handlers, help and command list. Plugins run in easyt's environment, so they use only its libraries, and read their own prompts with `load_prompt(name, directory)`. Tests see no plugins but their own
- **Service** ([service.py](src/easyt/service.py)) - `easyt install` writes `~/.config/systemd/user/easyt-bot.service`, which runs `easyt bot` of the venv that installed it in `~/.easyt`, restarted on failure; the other service commands wrap `systemctl --user` and `journalctl --user`

### Core Components

- **YouTube** ([youtube.py](src/easyt/youtube.py)) - `parse_youtube_url()` reads which video or playlist a URL names; `Loader` downloads YouTube videos and transcripts via yt-dlp, handles playlist extraction. The transcript is the caption track in the video's own language, never a machine translation, which `_original_track()` picks (copied from max); `loader.lang` names that language for the pipeline
- **LLM backend** ([backend/](src/easyt/backend/)) - `open_backend()` reads `LLM_BACKEND` and returns the `Backend` ([base.py](src/easyt/backend/base.py)) that punctuation, translation, line breaking and aterm term extraction run on. `claude-code` or `codex` runs an agent CLI as a plain model on the user's subscription ([claude_code.py](src/easyt/backend/claude_code.py), [codex.py](src/easyt/backend/codex.py)) at high effort: Claude Code on `opus`, the CLI's alias for the latest Opus, and Codex on a model named in full, as Codex has no such alias. Any other value is a pydantic-ai `provider:model` name, such as `anthropic:claude-opus-5-5`, `openai:gpt-6.1-sol` or `openrouter:google/gemini-3-pro`, that [pydantic_ai.py](src/easyt/backend/pydantic_ai.py) runs on that provider's API through a pydantic-ai agent at high effort (`thinking="high"`), with no cap below the model's own output limit. Every backend answers a single prompt (`ask()`) or the turns of a `session()`, which share one history: one CLI process, one Codex thread, or a pydantic-ai conversation. A failed call raises `BackendError` (or the API's own error); nothing falls back to another backend. Unset means `claude-code`.
- **Stages** ([stages/](src/easyt/stages/)) - The LLM stages the standard pipeline runs, a module each; the `stage` that `open_backend()` takes names them
  - **Punctuator** ([punctuate.py](src/easyt/stages/punctuate.py)) - Uses LLM to add punctuation to auto-generated transcripts, uses Levenshtein distance for character-level timestamp mapping. It first cuts the raw transcript into even chunks of about `chunk_size` letters, each cut after the word where SaT (`sat-12l-sm`, which reads unpunctuated text; on the GPU when there is one) is surest a sentence ends, so no chunk waits for the one before it. The chunks are then punctuated `LLM_CONCURRENCY` at a time, the model putting each sentence on a line of its own, a quotation staying in the sentence that frames it. SaT splits only a chunk whose answer has nothing in its `<output>` block, an answer left on one line, and a line over 1000 characters
  - **Translator** ([translate.py](src/easyt/stages/translate.py)) - Translates a chunk of sentences on `LLM_BACKEND`; a sentence the answer leaves out keeps its source text. The standard pipeline translates its punctuation chunks `LLM_CONCURRENCY` at a time
  - **LLMLineBreaker** ([line_break.py](src/easyt/stages/line_break.py)) - Breaks each translated text into lines and groups the lines into screens, what one subtitle shows at once: 1 to 3 lines, as many as the meaning asks. Texts go out as `<segment id lines>` items (XML-escaped), ten per call and `LLM_CONCURRENCY` calls at a time; an answer's segment holds its lines with a `---` line between screens, and blank lines mean nothing. A text the answer leaves out or changes, as a cut-off answer does, stays whole on one line; a screen over 3 lines is split into even screens of at most 3
- **Prompts** ([prompts/](src/easyt/prompts/)) - TOML-based chat prompts in `*_prompts.toml`, loaded via `load_prompt()` (from a plugin's own directory when it names one) as `str.format` templates that `format()` turns into the system text and each turn's prompt

### Key Technical Patterns

- **Runtime home**: `.env`, the Google Drive credentials and `outputs/` live in `~/.easyt` (`EASYT_HOME` in [\_\_init\_\_.py](src/easyt/__init__.py)), whatever directory easyt runs in; the repository holds code only.
- **Model calls at once**: punctuation, translation, line breaking and aterm's term extraction hand their calls to `run_concurrently()` ([concurrency.py](src/easyt/stages/concurrency.py)), which runs `LLM_CONCURRENCY` of them at a time, keeps their order and counts them done on the stage's span. After a call fails no other one starts, but those already running finish, so their answers are cached; then the first error is raised as it is. A cancel, such as the bot stopping, still stops them all.
- **Blocking work off the event loop**: work that runs long enough to stall the bot goes through `in_thread()` ([threads.py](src/easyt/utils/threads.py)), so the bot keeps answering during a job: yt-dlp, Drive transfers, SaT, and the timestamp mapping of a whole video. Work of milliseconds, such as aligning one chunk, stays on the loop: easyt has one user, and a thread there only adds complexity. A thread cannot be cancelled, so a cancelled task stops its thread at the next `checkpoint()`: yt-dlp's progress hook, and each 8 MB chunk of a Drive transfer. The threads are daemons, so Ctrl+C or a service stop does not wait for work that has no checkpoint.
- **Log output**: work runs in spans ([spans.py](src/easyt/utils/spans.py)): `with span(name)` around a job, a video and each stage makes a tree, which the context carries into tasks and threads. A record belongs to the innermost span open where it is logged; a span's start, its end (what it came to, its counts such as model `calls` and their `cost`, its time) and, every `HEARTBEAT_SECONDS` at most, its progress are logged too. stderr shows the records as an indented tree, the same on a terminal and in journald: colored on a terminal, each line led by its syslog level (`<4>`) in the journal, and no live bars anywhere. The bot's message is the live view of the same spans. Messages are plain text, no markup. The level is INFO; `-v` adds DEBUG, `-q` keeps successes, warnings and errors.
- **utils/** ([utils/](src/easyt/utils/)): only modules that import nothing from the rest of easyt (threads, spans and logging, text alignment), which [test_utils.py](tests/test_utils.py) checks. Code that needs a pipeline, a model, YouTube or Drive stays with that code instead.
- **Chunk-level caching**: Punctuation and translation results are cached per chunk in the output directory. Processing resumes from where it left off: punctuation keeps each chunk's answer in `punctuate_cache_N.txt` the moment it arrives, and its cuts in `punctuate_cuts.json` (SaT on another device may cut elsewhere), all removed once `punctuate_results.json` is written; an answer with nothing in its `<output>` block is not kept, so a rerun asks again. Translation names its files by target language (`translate_cache_<lang>_N.txt`), as every run of a video on one day shares its folder.
- **Timestamp mapping**: Uses Levenshtein distance matching blocks to map punctuated/translated text back to original subtitle timestamps at character level.
- **Languages**: codes and names come from CLDR through Babel ([languages.py](src/easyt/languages.py)): `language_tag()` turns `-t` into a tag such as `pt-BR`, `language_name()` names a code in the prompts, and `host_language()` reads the locale, English for the C locale.
- **Prompt templates**: Every stage's LLM prompt is in [prompts/](src/easyt/prompts/) `*_prompts.toml` files using TOML array-of-tables format (`[[section.subsection]]`). The backend's own short follow-up stays in its code: asking again for a structured block that could not be read.
- **Reading model output**: pick per call. Lists of items are XML read by `XmlItemParser`: translated and line-break segments, matched back by id, and aterm's terms ([xml_parser.py](src/easyt/parser/xml_parser.py)); a small typed answer is a Pydantic model read by `Session.ask_structured()`, whose format guide and parser come from [structured.py](src/easyt/parser/structured.py) (copied from the max project).

### Processing Flow (Standard Pipeline)

1. Load video metadata and transcript from YouTube, in the video's own language
2. Punctuate raw transcript on `LLM_BACKEND` (chunked processing)
3. Translate sentences on `LLM_BACKEND` (chunked processing), unless the video is already in the target language
4. Map translated sentences back to original timestamps using edit distance
5. Break lines and group them into screens on `LLM_BACKEND`, then generate the SRT file with one subtitle per screen: each gets a second first (`MIN_SCREEN_SECONDS`, as far as the text's time allows), and the rest of the text's time by its share of the letters

## Environment Variables

Required in `~/.easyt/.env`:
- `ANTHROPIC_API_KEY` - For Claude on the API: `LLM_BACKEND=anthropic:…`
- `OPENAI_API_KEY` - For OpenAI models on the API: `LLM_BACKEND=openai:…`
- `OPENROUTER_API_KEY` - Optional, for `LLM_BACKEND=openrouter:…`
- `LLM_BACKEND` - Optional, where punctuation, translation, line breaking and aterm term extraction run: `claude-code` on the Claude subscription (needs `claude` logged in), `codex` on the ChatGPT subscription (needs `codex` logged in with ChatGPT), or a pydantic-ai `provider:model` name such as `anthropic:claude-opus-5-5`; unset means `claude-code`
- `MAX_LINE_LEN` - Optional, letters and digits a subtitle line holds; unset means 20, which fits Korean, Japanese and Chinese, and an alphabet such as English takes about 36
- `LLM_CONCURRENCY` - Optional, how many model calls punctuation, translation, line breaking and aterm's term extraction each run at once; unset means 4
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - The bot's token, and the one chat it answers and sends results to
- `GOOGLE_OAUTH_CREDENTIALS_FILE` - Optional, path to the Google OAuth credentials JSON file for Drive upload, relative to `~/.easyt`. The token is kept beside it; `easyt auth` makes it, and a YouTube run checks it before any work and fails with `DriveAuthError` rather than opening a browser
- `GOOGLE_DRIVE_FOLDER_ID` - Optional, target Google Drive folder ID for uploading results

## Output Structure

Results are saved to `~/.easyt/outputs/`, organized by video title with date prefix. Each video directory contains:
- Original transcript JSON
- Punctuation results
- Translation cache files (per chunk and target language)
- Final SRT subtitle file, `<title>.<lang>.srt`
