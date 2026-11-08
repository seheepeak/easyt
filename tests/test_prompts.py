"""How the prompts are read from their TOML files."""

import pytest

from easyt.prompts import load_prompt


def test_a_table_two_prompt_files_hold_is_refused_rather_than_one_kept(tmp_path):
    (tmp_path / "a_prompts.toml").write_text('[[greet]]\ntype = "system"\ncontent = "Hi"\n')
    (tmp_path / "b_prompts.toml").write_text('[[greet]]\ntype = "system"\ncontent = "Hello"\n')
    with pytest.raises(ValueError, match="both hold greet"):
        load_prompt("greet", tmp_path)
