"""The head every HTML page easyt makes shares: the phone's width, the size of the type, and CSS
variables for the measure of a line and the fonts, which the page's own style picks from."""

from dominate.tags import meta, style
from dominate.util import raw

# 18px on a phone and 20px on a wider screen, in lines of about 70 letters, as news sites set.
# English in Georgia, or Apple's New York (ui-serif); what holds Korean in each system's sans, as
# Georgia has no Hangul and a serif's would be Windows' old Batang.
_CSS = """
:root {
  --measure: 33em;
  --english: ui-serif, Georgia, serif;
  --korean: -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", system-ui,
    "Apple SD Gothic Neo", "Malgun Gothic", sans-serif;
}
html { font-size: 18px; }
@media (min-width: 740px) { html { font-size: 20px; } }
"""


def page_head() -> None:
    """Add the shared head to the head being built, ahead of the page's own style."""
    meta(charset="UTF-8")
    # Without it, a phone lays the page out as wide as a desktop's and shrinks it to fit.
    meta(name="viewport", content="width=device-width, initial-scale=1")
    style(raw(_CSS))
