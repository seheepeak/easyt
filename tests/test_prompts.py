"""How the prompts are read from their TOML files."""

from easyt.prompts import load_prompt


def test_a_prompt_is_in_the_file_the_first_part_of_its_name_names(tmp_path):
    (tmp_path / "greet.toml").write_text(
        '[[greet.messages]]\ntype = "system"\ncontent = "Hi {name}"\n\n'
        '[[greet.formal.messages]]\ntype = "system"\ncontent = "Good day"\n'
    )
    assert load_prompt("greet", tmp_path).format(name="Ann") == ["Hi Ann"]
    assert load_prompt("greet.formal", tmp_path).format() == ["Good day"]
