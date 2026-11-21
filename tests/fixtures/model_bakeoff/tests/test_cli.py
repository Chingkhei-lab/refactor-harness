from calc.cli import main


def test_mean_command_prints_the_mean(capsys):
    assert main(["mean", "1", "2", "3"]) == 0
    assert capsys.readouterr().out.strip() == "2.0"


def test_uppercase_flag_upper_cases_the_output(capsys):
    assert main(["--uppercase", "slug", "Hello", "World"]) == 0
    assert capsys.readouterr().out.strip() == "HELLO-WORLD"
