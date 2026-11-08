# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EasyT is a YouTube video subtitle translation tool that downloads videos/transcripts, applies LLM-based punctuation, translates content (from the video's own language into the host's, as its locale names it), and generates SRT subtitle files. It also extracts advanced terms, reads articles with their translation and terms, and takes plugins that add commands of their own.

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
uv run easyt article <file>                    # An article in Markdown, with its translation and terms (HTML output)
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

- **Pipeline_subtitle** ([subtitle.py](src/easyt/pipeline/subtitle.py)) - Video subtitle translation: punctuation → LLM translation (Claude, or GPT through Codex) → timestamp mapping → SRT generation
- **Pipeline_aterm** ([aterm.py](src/easyt/pipeline/aterm.py)) - Advanced terms mode: extracts vocabulary with definitions, generates HTML output
- **Pipeline_article** ([article.py](src/easyt/pipeline/article.py)) - An article that [article.py](src/easyt/article.py) read from Markdown, translated from the language Lingua found it in: SaT splits each block into sentences (a heading stays whole, as SaT takes its capitalized words for sentence starts), then one stage translates the sentences, as an article's, and picks out each paragraph's advanced terms, the article's blocks cut into even chunks of about `CHUNK_SIZE` letters for both. The HTML shows a sentence's translation in a tooltip when it is clicked or tapped, until the next click (a drag that selects text shows nothing); where scripts do not run, as in Telegram's preview on iPhone, each sentence labels a hidden checkbox, and a tapped one's translation shows under it by CSS alone. Under each block, a `<details>` box holds the terms listed for it, a term two chunks list kept where it came first. A table shows as the article has it, untranslated and without terms, in a box that scrolls sideways when the table is wider than the page. Its images go in the page as data URLs, as Telegram's preview on iPhone loads nothing from the internet: `embed_images()` downloads each, asking for AVIF as a browser does (NYT's is a tenth of its PNG), and one that fails, or is over `MAX_IMAGE_BYTES`, keeps its URL. Its text, headings too, is in `--english`, and the tooltip and the terms boxes, which hold the Korean, in `--korean` (see Pages below). It does not use Pipeline_base, which is for videos
- **Pipeline_base** ([base.py](src/easyt/pipeline/base.py)) - Common functionality: punctuation, and sending a result to the Telegram chat through `send_document()` ([notify.py](src/easyt/notify.py))

All pipeline `run()` methods are async (entry point uses `asyncio.run()`).

### Entry Points

- **CLI** ([app.py](src/easyt/app.py)) - `easyt URL` translates; `easyt COMMAND` runs another mode, the bot, the Drive sign-in, or drives the bot's service. The YouTube modes go through `run_youtube()` ([jobs.py](src/easyt/jobs.py)): `parse_youtube_url()` reads the full video or playlist URL, the mode's pipeline runs on each video (translating into `-t`, or else `host_language()`, the locale's language as Babel reads it, which the bot always does), and the outputs are uploaded to Google Drive. `easyt article FILE` reads a Markdown file, which `article_to_translate()` ([jobs.py](src/easyt/jobs.py)) refuses as `read_article()` does, or when it is already in the host's language, and runs `run_article()`, which keeps the article and its outputs in a folder named like a video's, sends the HTML to the chat and uploads nothing. A plugin's command runs its own `run()`
- **Telegram bot** ([bot.py](src/easyt/bot.py)) - Answers only `TELEGRAM_CHAT_ID`. A message that is a YouTube URL (`is_youtube_url()`) is translated and answered with its Drive folder link, and one that is neither that nor an article gets the help; `/aterm` and `/transcript` have their pipelines send the HTML or TXT to the chat, and a plugin's `/NAME` queues its own `job()`. A text file (a `.md`, a `.txt` or any `text/` type) is an article, for one longer than the 4096 characters a message holds; a message is one when `article_part()` finds it whole, its front matter first and its `<!-- EOF -->` line last. Telegram cuts a longer text into messages, and no part of one runs: the first, with the front matter alone, is answered to send a file instead, the last, with the line alone, gets no answer, and a part between, with neither, is any other text. `run_article()` sends the HTML to the chat, and the `ValueError` of `article_to_translate()`, such as for an article of no text or one already in the host's language, is the bot's answer, before any job is queued. Jobs run one at a time as asyncio tasks in the bot's process, each with a message drawing the tree of its spans: a finished span a line, the running ones with how far they are (`14/47`, `64%`), the deepest with its last log lines; plus `/status`, `/cancel` and a Cancel button. A job running or waiting when the bot stops is dropped, not resumed
- **Plugins** ([plugins.py](src/easyt/plugins.py)) - A plugin is a package in `PLUGINS_DIR` whose `command.py` names `command`: a `Command` with the CLI's help, arguments and `run()`, and the bot's help line and `job()`, which a `ValueError` turns into the bot's answer. `commands()` imports them, leaving out with a warning one that fails to load; app.py adds each as a subcommand, and bot.py as a /command in its handlers, help and command list. Plugins run in easyt's environment, so they use only its libraries, and read their own prompts with `load_prompt(name, directory)`. Tests see no plugins but their own
- **Service** ([service.py](src/easyt/service.py)) - `easyt install` writes `~/.config/systemd/user/easyt-bot.service`, which runs `easyt bot` of the venv that installed it in `~/.easyt`, restarted on failure; the other service commands wrap `systemctl --user` and `journalctl --user`

### Core Components

- **Article** ([article.py](src/easyt/article.py)) - An article as a web clipper writes it in Markdown: YAML front matter of its details (`site`, `author`, `published`, `url`, `description`), the article under its `# title`, and an `<!-- EOF -->` line last, which a file may leave out with the front matter. `read_article()` reads the Markdown as CommonMark (markdown-it-py), with GitHub's tables, into blocks: headings (the first h1 the title), images (a paragraph of images alone, linked or not), tables (each cell as plain text), and paragraphs as plain text (links as their text, no marks of emphasis or escape), a list item a paragraph of its own, a numbered one keeping its number; code, rules and HTML are left out. The page shows the site, author and published date above the article, the description is its file's caption in the chat, and its `lang` is the language `detect_language()` finds its text in, which its translation and its terms take as their source. An article of no text, of front matter that is not YAML, or of no language Lingua can tell raises `ValueError`
- **YouTube** ([youtube.py](src/easyt/youtube.py)) - `parse_youtube_url()` reads which video or playlist a URL names; `Loader` downloads YouTube videos and transcripts via yt-dlp, handles playlist extraction. The transcript is the caption track in the video's own language, never a machine translation, which `_original_track()` picks (copied from max); `loader.lang` names that language for the pipeline
- **LLM backend** ([backend/](src/easyt/backend/)) - `open_backend()` reads `LLM_BACKEND` and returns the `Backend` ([base.py](src/easyt/backend/base.py)) that punctuation, translation, line breaking, and aterm's and an article's term extraction run on. `claude-code` or `codex` runs an agent CLI as a plain model on the user's subscription ([claude_code.py](src/easyt/backend/claude_code.py), [codex.py](src/easyt/backend/codex.py)) at high effort: Claude Code on `opus`, the CLI's alias for the latest Opus, and Codex on a model named in full, as Codex has no such alias. Any other value is a pydantic-ai `provider:model` name, such as `anthropic:claude-opus-5-5`, `openai:gpt-6.1-sol` or `openrouter:google/gemini-3-pro`, that [pydantic_ai.py](src/easyt/backend/pydantic_ai.py) runs on that provider's API through a pydantic-ai agent at high effort (`thinking="high"`), with no cap below the model's own output limit. Every backend answers a single prompt (`ask()`) or the turns of a `session()`, which share one history: one CLI process, one Codex thread, or a pydantic-ai conversation. A failed call raises `BackendError` (or the API's own error); nothing falls back to another backend. Unset means `claude-code`.
- **Stages** ([stages/](src/easyt/stages/)) - The LLM stages the subtitle pipeline runs, a module each; the `stage` that `open_backend()` takes names them
  - **Punctuator** ([punctuate.py](src/easyt/stages/punctuate.py)) - Uses LLM to add punctuation to auto-generated transcripts, uses Levenshtein distance for character-level timestamp mapping. It first cuts the raw transcript into even chunks of about `chunk_size` letters, each cut after the word where SaT (`sat-12l-sm`, which reads unpunctuated text; on the GPU when there is one) is surest a sentence ends, so no chunk waits for the one before it. The chunks are then punctuated `LLM_CONCURRENCY` at a time, the model putting each sentence on a line of its own, a quotation staying in the sentence that frames it. SaT splits only a chunk whose answer has nothing in its `<output>` block, an answer left on one line, and a line over 1000 characters
  - **Translator** ([translate.py](src/easyt/stages/translate.py)) - Translates a chunk of sentences on `LLM_BACKEND` with the one `translate` prompt, whose `{kind_notes}` says what kind of text they are (`translate.kinds`: a video's subtitles, said as a speaker would say them, or an article's sentences), and whose `{pair_notes}` takes what `translate.pairs` keeps for the pair of languages, such as `translate.pairs.en.ko`, or nothing; a sentence the answer leaves out keeps its source text. The subtitle pipeline translates its punctuation chunks `LLM_CONCURRENCY` at a time
  - **LLMLineBreaker** ([line_break.py](src/easyt/stages/line_break.py)) - Breaks each translated text into lines and groups the lines into screens, what one subtitle shows at once: 1 to 3 lines, as many as the meaning asks. Texts go out as `<segment id lines>` items (XML-escaped), ten per call and `LLM_CONCURRENCY` calls at a time; an answer's segment holds its lines with a `---` line between screens, and blank lines mean nothing. A text the answer leaves out or changes, as a cut-off answer does, stays whole on one line; a screen over 3 lines is split into even screens of at most 3
- **Prompts** ([prompts/](src/easyt/prompts/)) - A TOML file per LLM stage: [punctuate.toml](src/easyt/prompts/punctuate.toml), [translate.toml](src/easyt/prompts/translate.toml), [line_break.toml](src/easyt/prompts/line_break.toml), and [terms.toml](src/easyt/prompts/terms.toml) with `terms.video` for aterm and `terms.article` for an article. `load_prompt()` (from a plugin's own directory when it names one) reads a prompt's messages as `str.format` templates that `format()` turns into the system text and each turn's prompt; `load_text()` reads a text kept beside them, such as `translate.pairs.en.ko`

### Key Technical Patterns

- **Runtime home**: `.env`, the Google Drive credentials and `outputs/` live in `~/.easyt` (`EASYT_HOME` in [\_\_init\_\_.py](src/easyt/__init__.py)), whatever directory easyt runs in; the repository holds code only.
- **Model calls at once**: punctuation, translation, line breaking, aterm's term extraction, and an article's translation and terms together, hand their calls to `run_concurrently()` ([concurrency.py](src/easyt/stages/concurrency.py)), which runs `LLM_CONCURRENCY` of them at a time, keeps their order and counts them done on the stage's span. After a call fails no other one starts, but those already running finish, so their answers are cached; then the first error is raised as it is. A cancel, such as the bot stopping, still stops them all.
- **Blocking work off the event loop**: work that runs long enough to stall the bot goes through `in_thread()` ([threads.py](src/easyt/utils/threads.py)), so the bot keeps answering during a job: yt-dlp, Drive transfers, SaT, and the timestamp mapping of a whole video. Work of milliseconds, such as aligning one chunk, stays on the loop: easyt has one user, and a thread there only adds complexity. A thread cannot be cancelled, so a cancelled task stops its thread at the next `checkpoint()`: yt-dlp's progress hook, and each 8 MB chunk of a Drive transfer. The threads are daemons, so Ctrl+C or a service stop does not wait for work that has no checkpoint.
- **Log output**: work runs in spans ([spans.py](src/easyt/utils/spans.py)): `with span(name)` around a job, a video and each stage makes a tree, which the context carries into tasks and threads. A record belongs to the innermost span open where it is logged; a span's start, its end (what it came to, its counts such as model `calls` and their `cost`, its time) and, every `HEARTBEAT_SECONDS` at most, its progress are logged too. stderr shows the records as an indented tree, the same on a terminal and in journald: colored on a terminal, each line led by its syslog level (`<4>`) in the journal, and no live bars anywhere. The bot's message is the live view of the same spans. Messages are plain text, no markup. The level is INFO; `-v` adds DEBUG, `-q` keeps successes, warnings and errors.
- **utils/** ([utils/](src/easyt/utils/)): only modules that import nothing from the rest of easyt (threads, spans and logging, text alignment), which [test_utils.py](tests/test_utils.py) checks. Code that needs a pipeline, a model, YouTube or Drive stays with that code instead.
- **Pages**: the HTML pages, the article's, aterm's and a plugin's such as nseo's, start their head with `page_head()` ([page.py](src/easyt/pipeline/page.py)): the viewport at the phone's width, as without it a phone shrinks the page and its text, type of 18px on a phone and 20px from 740px wide, and CSS variables their own style picks from: `--measure` (33em, lines of about 70 letters), `--english` (Georgia, or New York through `ui-serif` on Apple devices) and `--korean` for what holds Korean (SF Pro and Apple SD Gothic Neo, or Segoe UI Variable and Malgun Gothic on Windows), as Georgia has no Hangul. The fonts are the systems' own, so a page loads nothing. Each page keeps its own colors and layout.
- **Chunk-level caching**: Punctuation and translation results are cached per chunk in the output directory. Processing resumes from where it left off: punctuation keeps each chunk's answer in `punctuate_cache_N.txt` the moment it arrives, and its cuts in `punctuate_cuts.json` (SaT on another device may cut elsewhere), all removed once `punctuate_results.json` is written; an answer with nothing in its `<output>` block is not kept, so a rerun asks again. Translation names its files by target language (`translate_cache_<lang>_N.txt`), as every run of a video on one day shares its folder. An article's caches are named by a digest of their chunk's text instead of its number (`translate_cache_<lang>_<digest>.txt`), as an article sent again that day lands in the same folder, perhaps changed.
- **Timestamp mapping**: Uses Levenshtein distance matching blocks to map punctuated/translated text back to original subtitle timestamps at character level.
- **Languages**: codes and names come from CLDR through Babel ([languages.py](src/easyt/languages.py)): `language_tag()` turns `-t` into a tag such as `pt-BR`, `language_name()` names a code in the prompts, `host_language()` reads the locale, English for the C locale, and `detect_language()` finds the language of a text with Lingua (`lingua-language-detector`, not the unrelated `lingua`), as an article does not say its own.
- **Prompt templates**: A prompt is in the TOML file that the first part of its name names (`terms.article` in terms.toml), as the array of tables `[[name.messages]]`, a message per table and the system message first: a session of several turns, such as aterm's, is one prompt whose messages are its turns in order. A text filled into a prompt is a plain table beside them, such as `[translate.kinds]` and `[translate.pairs.en]`. The backend's own short follow-up stays in its code: asking again for a structured block that could not be read.
- **Reading model output**: pick per call. Lists of items are XML read by `XmlItemParser`: translated and line-break segments, matched back by id, and aterm's and an article's terms ([xml_parser.py](src/easyt/parser/xml_parser.py)); a small typed answer is a Pydantic model read by `Session.ask_structured()`, whose format guide and parser come from [structured.py](src/easyt/parser/structured.py) (copied from the max project).

### Processing Flow (Subtitle Pipeline)

1. Load video metadata and transcript from YouTube, in the video's own language
2. Punctuate raw transcript on `LLM_BACKEND` (chunked processing)
3. Translate sentences on `LLM_BACKEND` (chunked processing), unless the video is already in the target language
4. Map translated sentences back to original timestamps using edit distance
5. Break lines and group them into screens on `LLM_BACKEND`, then generate the SRT file with one subtitle per screen: each gets a second first (`MIN_SCREEN_SECONDS`, as far as the text's time allows), and the rest of the text's time by its share of the letters

## Environment Variables

Set in `~/.easyt/.env`, which [.env.example](.env.example) is the template of (keep the two in step):
- `ANTHROPIC_API_KEY` - For Claude on the API: `LLM_BACKEND=anthropic:…`
- `OPENAI_API_KEY` - For OpenAI models on the API: `LLM_BACKEND=openai:…`
- `OPENROUTER_API_KEY` - Optional, for `LLM_BACKEND=openrouter:…`
- `LLM_BACKEND` - Optional, where punctuation, translation, line breaking, and aterm's and an article's term extraction run: `claude-code` on the Claude subscription (needs `claude` logged in), `codex` on the ChatGPT subscription (needs `codex` logged in with ChatGPT), or a pydantic-ai `provider:model` name such as `anthropic:claude-opus-5-5`; unset means `claude-code`
- `MAX_LINE_LEN` - Optional, letters and digits a subtitle line holds; unset means 20, which fits Korean, Japanese and Chinese, and an alphabet such as English takes about 36
- `LLM_CONCURRENCY` - Optional, how many model calls punctuation, translation, line breaking, aterm's term extraction, and an article's translation and terms together, each run at once; unset means 4
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` - The bot's token, and the one chat it answers and sends results to
- `GOOGLE_OAUTH_CREDENTIALS_FILE` - Optional, path to the Google OAuth credentials JSON file for Drive upload, relative to `~/.easyt`. The token is kept beside it; `easyt auth` makes it, and a YouTube run checks it before any work and fails with `DriveAuthError` rather than opening a browser
- `GOOGLE_DRIVE_FOLDER_ID` - Optional, target Google Drive folder ID for uploading results
- `PLUGINS_DIR` - Optional, the folder easyt loads plugins from, relative to `~/.easyt`; unset means `~/.easyt/plugins`

## Output Structure

Results are saved to `~/.easyt/outputs/`, organized by video title with date prefix. Each video directory contains:
- Original transcript JSON
- Punctuation results
- Translation cache files (per chunk and target language)
- Final SRT subtitle file, `<title>.<lang>.srt`

An article's folder there, `[<date>] (<site>) <title>`, holds the Markdown it came as (`article.md`), its translation and term caches per chunk, named by a digest of the chunk's text (`translate_cache_<lang>_<digest>.txt`, `terms_cache_<lang>_<digest>.txt`), and `<title>.html`.
