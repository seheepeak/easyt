"""What the command line asks for."""

import pytest

from easyt.app import parse_args, run

URL = "https://www.youtube.com/watch?v=Qmafkf_08uQ"


def test_a_url_alone_is_translated():
    args = parse_args([URL, "-t", "en"])
    assert (args.command, args.url, args.target_lang, args.resolution) == (
        "translate",
        URL,
        "en",
        "1080p",
    )


def test_the_target_language_is_any_code_cldr_knows():
    assert parse_args([URL, "-t", "pt_br"]).target_lang == "pt-BR"
    with pytest.raises(SystemExit):
        parse_args([URL, "-t", "korean"])


def test_a_command_comes_before_its_url():
    args = parse_args(["aterm", URL])
    assert (args.command, args.url) == ("aterm", URL)


async def test_a_plugin_command_takes_its_own_arguments_and_runs(echo_plugin):
    await run(parse_args(["echo", "hi"]))
    assert echo_plugin == ["hi"]


def test_the_service_has_commands_of_its_own():
    assert [parse_args([c]).command for c in ("install", "log", "restart")] == [
        "install",
        "log",
        "restart",
    ]
