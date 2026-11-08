"""Which language the host's locale names, and which one a text is in."""

import pytest

from easyt.languages import detect_language, host_language


@pytest.mark.parametrize(
    "env, code",
    [
        ({"LANGUAGE": "ko:en_US:en", "LANG": "en_US.UTF-8"}, "ko"),  # LANGUAGE before LANG
        ({"LC_ALL": "C.UTF-8", "LANG": "ja_JP.UTF-8"}, "en"),  # the C locale is English
        ({"LANG": "it_IT.UTF-8"}, "it"),  # any language CLDR knows
        ({"LANG": "zh_TW.UTF-8"}, "zh-Hant"),  # with the script it implies
        ({"LANG": "xx_YY.UTF-8"}, "en"),
        ({}, "en"),
    ],
)
def test_the_host_language_is_the_one_its_locale_names(monkeypatch, env, code):
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LC_CTYPE", "LANG"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    assert host_language() == code


@pytest.mark.parametrize(
    "text, code",
    [
        ("Hello world", "en"),  # even a text this short
        ("이 메모는 이번에 처음 알려졌다.", "ko"),
        ("Le mémo n'avait jamais été rendu public.", "fr"),
        ("中国情报机构招募了一名女商人。", "zh"),
        ("2026 38%", None),  # numbers have no language
    ],
)
def test_a_text_is_in_the_language_lingua_finds(text, code):
    assert detect_language(text) == code
