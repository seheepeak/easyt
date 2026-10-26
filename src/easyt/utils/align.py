import Levenshtein as lev


def char_map(src: str, dst: str) -> list[int]:
    """For each character of `dst`, the index in `src` it matches, or -1: the Levenshtein
    alignment, case aside."""
    # A key per character: casefold lengthens some (ß to ss), which would shift the positions.
    src_keys, dst_keys = [c.casefold() for c in src], [c.casefold() for c in dst]
    to_src = [-1] * len(dst)
    for i, j, n in lev.matching_blocks(lev.editops(src_keys, dst_keys), src_keys, dst_keys):
        to_src[j : j + n] = range(i, i + n)
    return to_src
