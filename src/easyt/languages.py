"""Language codes and the names prompts give them, from CLDR through Babel."""

from babel import Locale, UnknownLocaleError
from babel.core import default_locale, get_locale_identifier


def host_language() -> str:
    """The language of the host's locale as Babel reads it for messages, with the script the
    locale implies, such as ko or zh-Hant; English for the C locale or one CLDR does not know."""
    try:
        locale = Locale.parse(default_locale("LC_MESSAGES") or "en")
    except (UnknownLocaleError, ValueError):
        return "en"
    return get_locale_identifier((locale.language, None, locale.script), sep="-")


def language_tag(code: str) -> str:
    """`code`, such as ko, pt_br or zh-hant, as the tag of a language CLDR knows: ko, pt-BR,
    zh-Hant. A code it does not know raises ValueError."""
    try:
        return str(Locale.parse(code.replace("-", "_"))).replace("_", "-")
    except UnknownLocaleError as e:
        raise ValueError(f"Not a language CLDR knows: {code}") from e


def language_name(code: str) -> str:
    """The English name the prompts give a language, such as Korean or Chinese (Traditional); a
    code CLDR does not know names itself."""
    try:
        return Locale.parse(code.replace("-", "_")).english_name or code
    except (UnknownLocaleError, ValueError):
        return code
