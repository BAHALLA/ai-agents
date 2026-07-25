"""Tests for Slack (thread, participant) → ADK session mapping."""

USER = "U_ALICE"
OTHER = "U_BOB"


class TestSessionMap:
    def test_get_returns_none_for_unknown_thread(self, session_map):
        assert session_map.get("C_CHAN", "123.456", USER) is None

    def test_set_and_get(self, session_map):
        session_map.set("C_CHAN", "123.456", USER, "sess_abc")
        assert session_map.get("C_CHAN", "123.456", USER) == "sess_abc"

    def test_different_channels_are_independent(self, session_map):
        session_map.set("C_ONE", "123.456", USER, "sess_1")
        session_map.set("C_TWO", "123.456", USER, "sess_2")
        assert session_map.get("C_ONE", "123.456", USER) == "sess_1"
        assert session_map.get("C_TWO", "123.456", USER) == "sess_2"

    def test_different_threads_are_independent(self, session_map):
        session_map.set("C_CHAN", "111.000", USER, "sess_a")
        session_map.set("C_CHAN", "222.000", USER, "sess_b")
        assert session_map.get("C_CHAN", "111.000", USER) == "sess_a"
        assert session_map.get("C_CHAN", "222.000", USER) == "sess_b"

    def test_participants_in_one_thread_are_independent(self, session_map):
        """ADK scopes sessions by user id, so two speakers cannot share one."""
        session_map.set("C_CHAN", "111.000", USER, "sess_alice")
        session_map.set("C_CHAN", "111.000", OTHER, "sess_bob")
        assert session_map.get("C_CHAN", "111.000", USER) == "sess_alice"
        assert session_map.get("C_CHAN", "111.000", OTHER) == "sess_bob"

    def test_remove(self, session_map):
        session_map.set("C_CHAN", "123.456", USER, "sess_abc")
        session_map.remove("C_CHAN", "123.456", USER)
        assert session_map.get("C_CHAN", "123.456", USER) is None

    def test_remove_clears_every_participant_by_default(self, session_map):
        session_map.set("C_CHAN", "111.000", USER, "sess_alice")
        session_map.set("C_CHAN", "111.000", OTHER, "sess_bob")
        session_map.set("C_CHAN", "222.000", USER, "sess_other_thread")

        session_map.remove("C_CHAN", "111.000")

        assert session_map.get("C_CHAN", "111.000", USER) is None
        assert session_map.get("C_CHAN", "111.000", OTHER) is None
        assert session_map.get("C_CHAN", "222.000", USER) == "sess_other_thread"

    def test_remove_nonexistent_is_noop(self, session_map):
        session_map.remove("C_CHAN", "999.999")  # should not raise
        session_map.remove("C_CHAN", "999.999", USER)  # should not raise
