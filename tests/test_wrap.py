from easyt.utils.wrap import MAX_LINE, wrap


def test_a_text_that_fits_a_line_is_one_screen_of_one_line():
    assert wrap("  Good   morning.\n") == [["Good morning."]]


def test_a_line_ends_after_punctuation_near_the_even_split():
    text = "We went to the store on Monday, and then we drove home to see the kids."
    # The even split would end the first line on "and".
    assert wrap(text) == [
        ["We went to the store on Monday,", "and then we drove home to see the kids."]
    ]


def test_a_long_sentence_keeps_every_word_in_screens_of_at_most_two_lines():
    text = (
        "We've talked on this podcast about how we do not believe that leading-edge chips "
        "should go to China, and if there's any smuggling going on, that that is wrong and "
        "that it should be stopped and the government should crack down on that."
    )
    screens = wrap(text)
    lines = [line for screen in screens for line in screen]
    assert all(len(screen) <= 2 for screen in screens)
    assert all(len(line) <= MAX_LINE for line in lines)
    assert " ".join(lines) == text


def test_a_word_longer_than_a_line_has_a_line_to_itself():
    url = "https://github.com/karpathy/autoresearch-agent-memory"
    screens = wrap(f"You can find all of the code for this at {url} along with the notes.")
    lines = [line for screen in screens for line in screen]
    assert len(url) > MAX_LINE and url in lines
    assert all(len(line) <= MAX_LINE for line in lines if line != url)


def test_a_text_with_no_space_to_break_at_stays_whole():
    text = "日本語の字幕です。" * 6
    assert wrap(text) == [[text]]
