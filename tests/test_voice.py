"""The voice grammar (gate V1): words become commands without a model, and never a secret."""

import pytest

from fleet import voice

pytestmark = pytest.mark.no_db


def cmd(text):
    c = voice.parse(text)
    return None if c is None else c.to_dict()


@pytest.mark.parametrize("said, expect", [
    ("open wikipedia.org", {"kind": "open", "url": "https://wikipedia.org"}),
    ("Otwórz Allegro.pl/kategoria/RAM", {"kind": "open", "url": "https://allegro.pl/kategoria/RAM"}),
    ("go to http://localhost:8710/tasks", {"kind": "open", "url": "http://localhost:8710/tasks"}),
    ("open settings", {"kind": "click", "label": "settings"}),  # not a URL: a thing on the page
    ("open the third result", {"kind": "click", "nth": 3, "what": "result"}),
    ("kliknij drugą pozycję", {"kind": "click", "nth": 2, "what": "item"}),
    ("click 4 link", {"kind": "click", "nth": 4, "what": "link"}),
    ("click on the Log in button", {"kind": "click", "label": "Log in button"}),
    ("naciśnij Dalej", {"kind": "click", "label": "Dalej"}),
    ("search for tanie loty do Lizbony", {"kind": "search", "query": "tanie loty do Lizbony"}),
    ("znajdź raspberry pi 5", {"kind": "search", "query": "raspberry pi 5"}),
    ("type Dzień dobry!", {"kind": "type", "text": "Dzień dobry"}),  # a trailing ! is dropped as punctuation
    ("scroll", {"kind": "scroll", "direction": "down"}),
    ("przewiń w górę", {"kind": "scroll", "direction": "up"}),
    ("wyżej", {"kind": "scroll", "direction": "up"}),
    ("wstecz", {"kind": "back"}),
    ("naprzód", {"kind": "forward"}),
    ("tab 3", {"kind": "tab", "which": 3}),
    ("poprzednia zakładka", {"kind": "tab", "which": "prev"}),
    ("read aloud", {"kind": "read"}),
    ("przerwij", {"kind": "stop"}),
    ("zgadzam się", {"kind": "approve"}),
    ("nie zgadzam się", {"kind": "reject"}),
    ("press enter", {"kind": "key", "key": "Enter"}),
    ("escape", {"kind": "key", "key": "Escape"}),
    ("new task: check the weather in Gdańsk", {"kind": "task", "prompt": "check the weather in Gdańsk"}),
    ("co umiesz", {"kind": "help"}),
    ("  Scroll   Down.  ", {"kind": "scroll", "direction": "down"}),
])
def test_polish_and_english_utterances_become_commands(said, expect):
    assert cmd(said) == expect


@pytest.mark.parametrize("said", ["", "   ", "blah blah nothing", "the", "what is the capital of Poland"])
def test_what_the_grammar_does_not_understand_is_none_not_a_guess(said):
    assert voice.parse(said) is None


@pytest.mark.parametrize("said", [
    "type my password hunter2", "wpisz hasło kotek123", "type card number 4111 1111 1111 1111",
    "type 4111-1111-1111-1111", "wpisz 4111111111111111", "type pin 1234", "napisz kod cvv 123",
    "search for 4111 1111 1111 1111", "new task pay with card 4111 1111 1111 1111",
])
def test_a_secret_is_refused_and_never_repeated(said):
    c = voice.parse(said)
    assert c is not None and c.kind == "refused"
    for secret in ("hunter2", "kotek123", "4111", "1234", "123"):
        assert secret not in repr(c) and secret not in voice.describe(c) and secret not in str(c.args)


def test_a_password_mentioned_in_a_task_or_a_label_is_not_a_secret():
    # "change password" is a thing on a page; the word alone carries nothing to leak
    assert cmd("click change password") == {"kind": "click", "label": "change password"}
    assert cmd("new task change my password on allegro")["kind"] == "task"


def test_describe_says_the_command_back_but_never_what_was_typed():
    assert voice.describe(voice.parse("open the third result")) == "Opening the third result"
    assert voice.describe(voice.parse("scroll up")) == "Scrolling up"
    assert "hello" not in voice.describe(voice.parse("type hello world"))
    assert voice.describe(None) == "I didn't understand that."
    assert voice.describe(voice.parse("help")) == voice.HELP
