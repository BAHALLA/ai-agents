"""Tests for the Google Chat card builders."""

from google_chat_bot.cards import (
    build_confirmation_card,
    build_error_card,
    build_progress_card,
    build_triage_result_card,
    classify_status,
)

from orrery_core import LEVEL_CONFIRM, LEVEL_DESTRUCTIVE


def test_confirmation_card_structure():
    card = build_confirmation_card(
        tool_name="restart_pod",
        args={"name": "api", "ns": "prod"},
        reason="restarts a pod",
        level=LEVEL_CONFIRM,
        action_id="abc123",
        interactive_buttons=True,
    )
    assert card["cardId"] == "abc123"

    header = card["card"]["header"]
    assert "restart_pod" in header["title"]
    assert header["subtitle"] == "Safety Guardrail"

    widgets = card["card"]["sections"][0]["widgets"]
    reason_widget = next(
        w for w in widgets if "Reason" in w.get("textParagraph", {}).get("text", "")
    )
    assert "restarts a pod" in reason_widget["textParagraph"]["text"]

    args_widget = next(
        w for w in widgets if "Arguments" in w.get("textParagraph", {}).get("text", "")
    )
    assert "name=api" in args_widget["textParagraph"]["text"]
    assert "ns=prod" in args_widget["textParagraph"]["text"]

    # Verify the Approve/Deny buttons carry the exact action_id so the click
    # resolves precisely this pending action.
    button_widget = next(w for w in widgets if "buttonList" in w)
    buttons = button_widget["buttonList"]["buttons"]
    assert len(buttons) == 2
    approve, deny = buttons
    assert "Approve" in approve["text"]
    assert approve["onClick"]["action"]["function"] == "confirm_action"
    assert approve["onClick"]["action"]["parameters"] == [{"key": "action_id", "value": "abc123"}]
    assert "Deny" in deny["text"]
    assert deny["onClick"]["action"]["function"] == "deny_action"
    assert deny["onClick"]["action"]["parameters"] == [{"key": "action_id", "value": "abc123"}]

    # Requester-only hint is present.
    assert any(
        "Only the requester can approve" in w.get("textParagraph", {}).get("text", "")
        for w in widgets
    )


def test_confirmation_card_default_mode_uses_reply_instructions():
    """Pub/Sub-safe default: no buttons (clicks can't round-trip on a pull
    transport); the card asks for a reply in the thread instead."""
    card = build_confirmation_card(
        tool_name="restart_pod",
        args={"name": "api"},
        reason="restarts a pod",
        level=LEVEL_CONFIRM,
        action_id="abc123",
    )
    widgets = card["card"]["sections"][0]["widgets"]
    assert not any("buttonList" in w for w in widgets)
    texts = [w.get("textParagraph", {}).get("text", "") for w in widgets]
    assert any("Reply <b>approve</b>" in t for t in texts)
    assert any("Only the requester can approve" in t for t in texts)


def test_destructive_card_uses_warning_emoji():
    card = build_confirmation_card(
        tool_name="drop_topic",
        args={},
        reason="deletes Kafka data",
        level=LEVEL_DESTRUCTIVE,
        action_id="xyz",
    )
    title = card["card"]["header"]["title"]
    assert title.startswith("\u26a0")  # warning sign
    assert "DESTRUCTIVE" in title


def test_card_handles_empty_args():
    card = build_confirmation_card(
        tool_name="list_pods",
        args={},
        reason="",
        level=LEVEL_CONFIRM,
        action_id="id1",
    )
    widgets = card["card"]["sections"][0]["widgets"]
    # No reason widget when reason is empty.
    assert not any("Reason" in w.get("textParagraph", {}).get("text", "") for w in widgets)
    # Args widget still present with "none" placeholder.
    args_widget = next(
        w for w in widgets if "Arguments" in w.get("textParagraph", {}).get("text", "")
    )
    assert "none" in args_widget["textParagraph"]["text"]


def _all_widget_text(card: dict) -> str:
    lines: list[str] = []
    for section in card["card"].get("sections", []):
        for widget in section.get("widgets", []):
            tp = widget.get("textParagraph")
            if tp:
                lines.append(tp.get("text", ""))
    return "\n".join(lines)


class TestClassifyStatus:
    def test_none_is_pending(self):
        assert classify_status(None) == "pending"

    def test_empty_is_pending(self):
        assert classify_status("") == "pending"

    def test_red_cluster_is_fail(self):
        assert classify_status("cluster health is RED") == "fail"

    def test_crashloop_is_fail(self):
        assert classify_status("pod in CrashLoopBackOff") == "fail"

    def test_yellow_is_warn(self):
        assert classify_status("cluster is yellow") == "warn"

    def test_degraded_is_warn(self):
        assert classify_status("service degraded") == "warn"

    def test_healthy_is_ok(self):
        assert classify_status("everything green and healthy") == "ok"


class TestProgressCard:
    def test_shape_with_no_chips(self):
        card = build_progress_card(
            current_agent="kafka_health_checker",
            current_tool="list_consumer_groups",
            subsystem_chips={},
            remediation=None,
            elapsed_seconds=3.0,
        )
        assert card["cardId"] == "progress"
        assert "Investigating" in card["card"]["header"]["title"]
        text = _all_widget_text(card)
        assert "Checking Kafka" in text  # friendly agent label
        assert "list_consumer_groups" in text
        assert "Elapsed 3s" in text

    def test_chips_rendered_when_subsystem_reports(self):
        card = build_progress_card(
            current_agent="triage_summarizer",
            current_tool=None,
            subsystem_chips={
                "kafka_status": {"status": "ok", "summary": "all brokers up"},
                "k8s_status": {"status": "fail", "summary": "api pod crashlooping"},
            },
            remediation=None,
            elapsed_seconds=0.0,
        )
        text = _all_widget_text(card)
        assert "Kafka" in text and "all brokers up" in text
        assert "Kubernetes" in text and "api pod crashlooping" in text
        # Severity icons appear.
        assert "❌" in text  # ❌
        assert "✅" in text  # ✅

    def test_remediation_panel_shows_when_present(self):
        card = build_progress_card(
            current_agent="remediation_actor",
            current_tool=None,
            subsystem_chips={},
            remediation={"remediation_action": "restarting api deployment"},
            elapsed_seconds=0.0,
        )
        text = _all_widget_text(card)
        assert "Remediation" in text
        assert "restarting api deployment" in text


def _remediation_buttons(card):
    """Collect run_remediation buttons anywhere in the card."""
    found = []
    for section in card["card"]["sections"]:
        for widget in section.get("widgets", []):
            for button in widget.get("buttonList", {}).get("buttons", []):
                if button.get("onClick", {}).get("action", {}).get("function") == "run_remediation":
                    found.append(button)
    return found


class TestTriageResultCard:
    def _chips(self):
        return {
            "kafka_status": {"status": "ok", "summary": "all green"},
            "k8s_status": {"status": "fail", "summary": "api pod crashlooping"},
            "docker_status": {"status": "ok", "summary": "4 containers running"},
            "observability_status": {"status": "warn", "summary": "2 firing alerts"},
            "elasticsearch_status": {"status": "ok", "summary": "cluster green"},
        }

    def test_overall_fail_dominates(self):
        card = build_triage_result_card(
            subsystem_chips=self._chips(),
            triage_report="Overall degraded, k8s is critical.",
            user_role="operator",
        )
        assert "Critical" in card["card"]["header"]["subtitle"]

    def test_all_ok_shows_healthy(self):
        chips = {
            "kafka_status": {"status": "ok", "summary": "ok"},
            "k8s_status": {"status": "ok", "summary": "ok"},
        }
        card = build_triage_result_card(
            subsystem_chips=chips, triage_report="fine", user_role="viewer"
        )
        assert "healthy" in card["card"]["header"]["subtitle"].lower()

    def test_remediate_button_visible_for_operator_on_fail(self):
        card = build_triage_result_card(
            subsystem_chips=self._chips(),
            triage_report="bad",
            user_role="operator",
            interactive_buttons=True,
        )
        buttons = _remediation_buttons(card)
        assert len(buttons) == 1
        assert "remediation" in buttons[0]["text"].lower()

    def test_remediate_default_mode_uses_reply_instruction(self):
        card = build_triage_result_card(
            subsystem_chips=self._chips(),
            triage_report="bad",
            user_role="operator",
        )
        assert _remediation_buttons(card) == []
        assert "reply <b>remediate</b>" in _all_widget_text(card).lower()

    def test_remediate_button_hidden_for_viewer(self):
        card = build_triage_result_card(
            subsystem_chips=self._chips(),
            triage_report="bad",
            user_role="viewer",
        )
        assert _remediation_buttons(card) == []

    def test_remediate_button_hidden_when_healthy(self):
        chips = {
            "kafka_status": {"status": "ok", "summary": "ok"},
            "k8s_status": {"status": "ok", "summary": "ok"},
        }
        card = build_triage_result_card(
            subsystem_chips=chips, triage_report="fine", user_role="admin"
        )
        assert _remediation_buttons(card) == []

    def test_subsystem_sections_present(self):
        card = build_triage_result_card(
            subsystem_chips=self._chips(),
            triage_report="full summary",
            user_role="operator",
        )
        text = _all_widget_text(card)
        assert "Kafka" in text
        assert "Kubernetes" in text
        assert "Elasticsearch" in text
        assert "full summary" in text


def test_error_card_shape():
    card = build_error_card("Something went wrong")
    assert card["cardId"] == "error"
    text = _all_widget_text(card)
    assert "Something went wrong" in text
