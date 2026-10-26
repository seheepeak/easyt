"""Which language the host's locale names."""

import pytest

from easyt.languages import host_language


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
