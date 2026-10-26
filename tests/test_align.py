"""How the characters of a text map onto the text it was made from."""

from easyt.utils.align import char_map


def test_each_character_maps_to_the_one_it_matches_and_an_added_one_to_none():
    # Case aside, "Hello, world." is "hello world" with a comma and a period added.
    assert char_map("hello world", "Hello, world.") == [0, 1, 2, 3, 4, -1, 5, 6, 7, 8, 9, 10, -1]


def test_a_letter_whose_fold_is_longer_keeps_the_positions_of_the_text():
    # "İ" lowers to two characters and "ß" casefolds to "ss"; neither may shift what follows.
    src, dst = "İstanbul STRASSE straße", "İstanbul strasse, Straße."
    to_src = char_map(src, dst)
    assert len(to_src) == len(dst) and max(to_src) < len(src)
    assert [dst[j] for j, i in enumerate(to_src) if i < 0] == [",", "."]


def test_a_word_left_out_is_all_of_the_source_left_unmatched():
    src = "so we we went"
    to_src = char_map(src, "So we went")
    # Which "we" goes is the alignment's choice; that every other character maps, in order, is not.
    assert -1 not in to_src
    assert to_src == sorted(set(to_src))
    assert len(src) - len(to_src) == 3
