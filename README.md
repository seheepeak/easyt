# EasyT

EasyT turns a YouTube video into subtitles in another language. It downloads the video and its caption track, has an LLM punctuate the raw captions, translates the sentences, maps them back onto the original timestamps, and has the LLM break them into readable subtitle screens. The result is an SRT file next to the video.

It also has two modes for learners of English:

- `aterm` lists the advanced terms of a video, with their definitions.
- `transcript` gives the punctuated transcript.

Everything can run from the command line, or from a Telegram bot that runs as a systemd user service.

## Setup

You need Python 3.12, [uv](https://docs.astral.sh/uv/) and ffmpeg.

```bash
uv sync
```

Settings and results live in `~/.easyt`, whatever directory you run from:

```
~/.easyt/
  .env           # API keys, LLM_BACKEND, Telegram and Google Drive settings
  .credentials/  # the Google OAuth client_secret.json, and gdrive_token.json
  outputs/       # results
```

The LLM calls run on `LLM_BACKEND`:

- `claude-code` (the default): your Claude subscription, through a logged-in `claude` CLI.
- `codex`: your ChatGPT subscription, through a logged-in `codex` CLI.
- A pydantic-ai `provider:model` name, such as `anthropic:claude-opus-5-5` or `openrouter:google/gemini-3-pro`, with that provider's API key.

[CLAUDE.md](CLAUDE.md) lists every setting.

## Usage

`uv run easyt -h` lists the commands, and `uv run easyt <command> -h` shows a command's options.

### Translation (the default)

Quote the URL: `?`, `&` and `[ ]` are shell syntax.

```bash
uv run easyt "https://www.youtube.com/watch?v=Qmafkf_08uQ"       # one video
uv run easyt "https://www.youtube.com/playlist?list=<id>"        # a whole playlist
uv run easyt "https://www.youtube.com/playlist?list=<id>[0:5]"   # part of a playlist (a Python slice)
```

| Option | Meaning |
|---|---|
| `-t ko` | the language to translate into: ko, en, ja, pt-BR, zh-Hant, ... (default: the host locale's language) |
| `--resolution 720p` | video quality: best, 1440p, 1080p (default), 720p |
| `-q` | only report successes, warnings and errors |

The video and the SRT are saved in `~/.easyt/outputs/` and uploaded to Google Drive.

### Other modes

```bash
uv run easyt aterm "<URL>"        # an English video's advanced terms and their meanings, as HTML sent to Telegram
uv run easyt transcript "<URL>"   # the punctuated transcript, as a TXT file sent to Telegram
```

Options come after the command: `uv run easyt aterm -q "<URL>"`.

### Google Drive sign-in

```bash
uv run easyt auth   # sign in in the browser; the token is saved in ~/.easyt/.credentials/
```

If the sign-in is missing or expired, a job stops right at its start and asks you to run `easyt auth`.

### Telegram bot (the systemd user service `easyt-bot`)

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `~/.easyt/.env`. The bot answers only that chat.

```bash
uv run easyt install     # install the service and start it, now and at every boot
uv run easyt status      # its state
uv run easyt log         # follow its log (journalctl --user -u easyt-bot -f)
uv run easyt restart
uv run easyt stop
uv run easyt start
uv run easyt uninstall   # remove the service (~/.easyt stays)
uv run easyt bot         # run the bot in the terminal instead (refused while the service runs)
```

- After a change to the code, `.env` or the dependencies, run `restart`. After a change to the unit (`service.py`) or a move of the venv, run `install` again.
- A restart drops the running job and the waiting ones, so check with `/status` that nothing is running first.

In the bot's chat:

- Send a YouTube URL to translate it. When it is done, the bot answers with the Google Drive folder.
- `/aterm URL` and `/transcript URL` run the other modes.
- `/status` shows the running job and the waiting ones, and `/cancel` cancels the running job. The Cancel button on a job's message does the same.

## Plugins

A separate package can add a command to both the command line (`easyt NAME ...`) and the bot (`/NAME`). It declares an `easyt.plugins.Command` under the `easyt.commands` entry point group:

```toml
[project.entry-points."easyt.commands"]
mycommand = "my_plugin.command:command"
```

The `Command` holds the command line's help, arguments and run, and the bot's help line and job. Install the plugin in the same environment as easyt. To give the bot service the plugin, run `easyt install` from that environment.

## License

[MIT](LICENSE)
