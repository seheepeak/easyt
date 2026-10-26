import pytest

from easyt.parser import DelimitedOutputParser


@pytest.mark.parametrize("delim", ["```", '"""', "|"])
def test_a_delimiter_that_opens_and_closes_alike_is_refused(delim):
    with pytest.raises(ValueError, match="neither a tag name nor a bracket pair"):
        DelimitedOutputParser(delim)


@pytest.mark.parametrize(
    "delim, text", [("output", "<output> hi </output>"), ("[]", "say [ hi ] twice")]
)
def test_a_tag_or_a_bracket_pair_is_read_between_its_ends(delim, text):
    assert DelimitedOutputParser(delim).parse(text) == "hi"
