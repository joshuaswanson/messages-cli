from messages_cli import messenger_api as m


def _thread(thread_id, ts, name=None):
    args = [[19, str(ts)], [19, str(ts)], "snippet", name, None, None, None, [19, str(thread_id)]]
    return [5, "deleteThenInsertThread", *args]


def test_find_calls_converts_lightspeed_values():
    payload = {"step": [1, [5, "verifyContactRowExists", [19, "100"], None, None, "Alice"], [[9]]]}
    calls = m._find_calls(payload)
    assert calls["verifyContactRowExists"] == [[100, None, None, "Alice"]]


def test_parse_inbox_names_threads():
    me, alice, bob, carol = 1, 100, 200, 300
    payload = {"step": [
        [5, "verifyContactRowExists", [19, str(alice)], None, None, "Alice"],
        [5, "verifyContactRowExists", [19, str(bob)], None, None, "Bob"],
        [5, "verifyContactRowExists", [19, str(carol)], None, None, "Carol"],
        _thread(10, 1000),
        _thread(20, 2000, name="Climbing"),
        _thread(30, 3000),
        [5, "addParticipantIdToGroupThread", [19, "10"], [19, str(me)]],
        [5, "addParticipantIdToGroupThread", [19, "10"], [19, str(alice)]],
        [5, "addParticipantIdToGroupThread", [19, "30"], [19, str(bob)]],
        [5, "addParticipantIdToGroupThread", [19, "30"], [19, str(carol)]],
        [5, "upsertMessage", "hey there", None, None, [19, "10"], None, [19, "1500"], None, None, "mid.1", None, [19, str(alice)]],
    ]}
    inbox = m._parse_inbox(payload, my_user_id=me)
    assert inbox.thread_name(10) == "Alice"
    assert not inbox.threads[10]["is_group"]
    assert inbox.thread_name(20) == "Climbing"
    assert inbox.thread_name(30) == "Bob, Carol"
    assert inbox.threads[30]["is_group"]
    assert [msg["text"] for msg in inbox.messages] == ["hey there"]
