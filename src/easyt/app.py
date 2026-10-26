# Suppress SyntaxWarning from docopt (unmaintained transitive dep: wtpsplit → mosestokenizer → docopt)
# docopt 0.6.2 uses unescaped regex strings, triggering warnings on Python 3.12+
import warnings

warnings.filterwarnings("ignore", category=SyntaxWarning, message="invalid escape sequence")

import argparse
import asyncio
import logging
import sys

from easyt import SUCCESS, plugins, setup_logging
from easyt.languages import host_language, language_tag

URL_HELP = "a YouTube video URL, or a playlist URL that may end in [start:end]"
SERVICE_COMMANDS = {
    "install": "Run the Telegram bot as a systemd user service, from now on and at every boot",
    "uninstall": "Stop the service and remove it",
    "start": "Start the service",
    "stop": "Stop the service",
    "restart": "Restart the service, as after a code change",
    "status": "Show the state of the service",
    "log": "Follow the log of the service",
}


def parse_args(argv: list[str]) -> argparse.Namespace:
    """`easyt URL` translates; `easyt COMMAND ...` runs COMMAND."""
    verbosity = argparse.ArgumentParser(add_help=False)
    verbosity.add_argument(
        "-v", "--verbose", action="store_true", help="Verbose output, with the debug lines"
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="Only report successes, warnings and errors"
    )

    commands = argparse.ArgumentParser(prog="easyt")
    sub = commands.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, about in (
        ("aterm", "Advanced terms of an English video with their definitions, as HTML"),
        ("transcript", "The punctuated transcript of a video, as a txt file"),
    ):
        command = sub.add_parser(name, parents=[verbosity], help=about, description=about)
        command.add_argument("url", metavar="URL", help=URL_HELP)
    for plugin in plugins.commands().values():
        command = sub.add_parser(
            plugin.name, parents=[verbosity], help=plugin.about, description=plugin.about
        )
        plugin.add_arguments(command)
    sub.add_parser("bot", parents=[verbosity], help="Run the Telegram bot here, in the foreground")
    sub.add_parser("auth", help="Sign in to Google Drive")
    for name, about in SERVICE_COMMANDS.items():
        sub.add_parser(name, help=about, description=about)

    if argv[:1] and argv[0] in sub.choices:
        return commands.parse_args(argv)

    translate = argparse.ArgumentParser(
        prog="easyt",
        parents=[verbosity],
        usage="%(prog)s [options] URL\n       %(prog)s COMMAND ...",
        description="Subtitles in another language for the YouTube video or playlist at URL.",
        epilog=f"commands: {', '.join(sub.choices)} (easyt COMMAND -h tells more)",
    )
    translate.add_argument("url", metavar="URL", help=URL_HELP)
    translate.add_argument(
        "-t",
        "--target-lang",
        type=language_tag,
        help="the language to translate into, as a code such as ko, pt-BR or zh-Hant "
        f"(default: the locale's, {host_language()} here)",
    )
    translate.add_argument(
        "--resolution",
        default="1080p",
        type=str,
        choices=["best", "1440p", "1080p", "720p"],
    )
    return translate.parse_args(argv, namespace=argparse.Namespace(command="translate"))


async def run(args: argparse.Namespace) -> None:
    if plugin := plugins.commands().get(args.command):
        await plugin.run(args)
        return

    from easyt.jobs import run_youtube
    from easyt.youtube import parse_youtube_url

    try:
        parse_youtube_url(args.url)
    except ValueError as e:
        sys.exit(str(e))
    if args.command == "translate":
        await run_youtube(
            args.url,
            "translate",
            target_lang=args.target_lang,
            resolution=args.resolution,
        )
    else:
        await run_youtube(args.url, args.command)


def main():
    args = parse_args(sys.argv[1:])

    if args.command in SERVICE_COMMANDS:
        from easyt import service

        if args.command == "install":
            service.install()
        elif args.command == "uninstall":
            service.uninstall()
        elif args.command == "log":
            service.follow_log()
        else:
            service.systemctl(args.command)
        return

    if args.command == "auth":
        from easyt.gdrive import GDriveUploader

        uploader = GDriveUploader()
        if uploader.credentials_file is None:
            sys.exit("GOOGLE_OAUTH_CREDENTIALS_FILE is not set in ~/.easyt/.env")
        uploader.login()
        return

    setup_logging(logging.DEBUG if args.verbose else SUCCESS if args.quiet else logging.INFO)

    if args.command == "bot":
        from easyt import bot, service

        if service.running_elsewhere():
            sys.exit(f"The {service.UNIT} service is running: `easyt stop` it first")
        bot.run()
        return

    from easyt.gdrive import DriveAuthError

    try:
        asyncio.run(run(args))
    except DriveAuthError as e:
        sys.exit(str(e))
