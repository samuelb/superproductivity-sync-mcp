import re

from superproductivity_sync_mcp.ids import generate_client_id, is_usable_client_id, is_valid_client_id, nanoid, uuid7


def test_nanoid():
    v = nanoid()
    assert len(v) == 21 and re.fullmatch(r"[A-Za-z0-9_-]{21}", v)


def test_uuid7_shape_and_order():
    a = uuid7(1_700_000_000_000)
    b = uuid7(1_700_000_000_001)
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", a)
    assert a < b


def test_client_id():
    cid = generate_client_id()
    assert cid.startswith("M_") and len(cid) == 8 and is_valid_client_id(cid)
    assert not is_valid_client_id("ab")
    assert is_valid_client_id("E_abc123")


def test_usable_client_id_is_stricter_than_the_reader_predicate():
    # The app's isValidClientIdFormat accepts any 10+ character string so legacy
    # ids are never orphaned; an id *we* put on the wire must have the minted shape.
    assert is_valid_client_id("hello world!") and not is_usable_client_id("hello world!")
    assert is_valid_client_id("E_abc123") and is_usable_client_id("E_abc123")
    assert not is_usable_client_id("a b c d") and not is_usable_client_id("abcd") and not is_usable_client_id(None)
    assert is_usable_client_id(generate_client_id())
