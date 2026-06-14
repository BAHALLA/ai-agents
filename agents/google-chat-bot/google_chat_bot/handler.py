"""Bridge between Google Chat events and the ADK Agent Runner."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from google.adk.runners import Runner
from google.genai import types

from orrery_core import extract_reply_text, set_user_role

from .cards import build_error_card, build_progress_card, build_triage_result_card
from .chat_client import ChatClient
from .config import GoogleChatBotConfig
from .confirmation import (
    ConfirmationStore,
    end_request_buffer,
    start_request_buffer,
)
from .progress import ProgressTracker

logger = logging.getLogger("google_chat_bot.handler")

# Events that trigger a full agent run. These may exceed Google Chat's
# ~30 second synchronous budget and should be deferred to a background
# task when a ``ChatClient`` is available.
_LONG_RUNNING_EVENTS = {"MESSAGE", "CARD_CLICKED", "APP_COMMAND"}


def wrap_for_addons(text: str, cards: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Wrap a response in the Workspace Add-ons DataActions schema.

    When a bot is routed via the Add-ons infrastructure (gsuiteaddons),
    it expects a response matching the RenderActions or DataActions schema.
    To simply reply with a message, we use ``hostAppDataAction.chatDataAction``.
    """
    message: dict[str, Any] = {"text": text}
    if cards:
        message["cardsV2"] = cards

    return {"hostAppDataAction": {"chatDataAction": {"createMessageAction": {"message": message}}}}


def empty_ack() -> dict[str, Any]:
    """Return an async acknowledgement for Workspace Add-ons.

    Returns the documented hostAppDataAction with actionStatus: OK.
    This acknowledges the interaction without posting a new message
    synchronously, avoiding 'code 3 — invalid response' errors.
    """
    return {"hostAppDataAction": {"chatDataAction": {"actionStatus": {"statusCode": "OK"}}}}


class GoogleChatHandler:
    """Handles incoming Google Chat events and delegates to an ADK Runner."""

    def __init__(
        self,
        runner: Runner,
        config: GoogleChatBotConfig,
        store: ConfirmationStore | None = None,
        chat_client: ChatClient | None = None,
    ):
        self.runner = runner
        self.config = config
        self.store = store or ConfirmationStore()
        # When chat_client is None, the handler stays in the legacy
        # synchronous path — useful for tests and local dev.
        self.chat_client = chat_client
        # Track fire-and-forget tasks so they don't get garbage-collected
        # before completion and so tests can await them.
        self._background_tasks: set[asyncio.Task[Any]] = set()

    def resolve_role(self, email: str) -> str:
        """Resolve RBAC role from user email (case-insensitive)."""
        normalized = (email or "").lower()
        if normalized in self.config.admin_emails:
            return "admin"
        if normalized in self.config.operator_emails:
            return "operator"
        return "viewer"

    async def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a Google Chat event.

        Supports standard Chat API events and Workspace Add-ons events.
        """
        logger.info("Processing Google Chat event: %s", event)

        # 1. Standard Chat API uses top-level 'type'.
        event_type = event.get("type")

        # 2. Workspace Add-ons use a different structure (no top-level type).
        chat = event.get("chat") or {}
        common = event.get("commonEventObject") or {}

        # Detect APP_COMMAND (Quick Commands / Slash Commands)
        if chat.get("appCommandPayload"):
            logger.info("Detected APP_COMMAND event")
            if self._should_defer("APP_COMMAND"):
                logger.info("Deferring APP_COMMAND to background task")
                self._spawn_background(self._handle_app_command_async(event))
                return empty_ack()
            return await self._handle_app_command(event)

        # Detect MESSAGE
        if event_type == "MESSAGE" or chat.get("messagePayload"):
            logger.info("Detected MESSAGE event")
            if self._should_defer("MESSAGE"):
                logger.info("Deferring MESSAGE to background task")
                self._spawn_background(self._handle_message_async(event))
                return empty_ack()
            return await self._handle_message(event)

        # Detect CARD_CLICKED.
        if event_type == "CARD_CLICKED" or common.get("invokedFunction") in (
            "confirm_action",
            "deny_action",
            "run_remediation",
        ):
            logger.info("Detected CARD_CLICKED event")
            if self._should_defer("CARD_CLICKED"):
                logger.info("Deferring CARD_CLICKED to background task")
                self._spawn_background(self._handle_card_click_async(event))
                return empty_ack()
            return await self._handle_card_click(event)

        # Detect ADDED_TO_SPACE — only when no message and no click.
        if event_type == "ADDED_TO_SPACE" or (chat.get("space") and not chat.get("messagePayload")):
            logger.info("Detected ADDED_TO_SPACE event")
            return self._wrap_for_addons("Thanks for adding me! Mention me to start investigating.")

        logger.warning("Unrecognized event structure: %s", event)
        return self._wrap_for_addons("I'm not sure how to handle this event type.")

    # ── Internal helpers ─────────────────────────────────────────────

    def _wrap_for_addons(
        self, text: str, cards: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Instance alias for :func:`wrap_for_addons` — kept for convenience."""
        return wrap_for_addons(text, cards)

    def _should_defer(self, event_type: str) -> bool:
        """True when the event should run in a background task."""
        return self.chat_client is not None and event_type in _LONG_RUNNING_EVENTS

    def _spawn_background(self, coro: Any) -> asyncio.Task[Any]:
        """Schedule *coro* as a tracked background task."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    @staticmethod
    def _extract_space_name(event: dict[str, Any]) -> str:
        """Robustly extract the space resource name from any event shape."""
        # Exhaustive priority list of where 'space' might be.
        chat = event.get("chat") or {}
        app_payload = chat.get("appCommandPayload") or {}
        msg_payload = chat.get("messagePayload") or {}
        message = (
            event.get("message") or msg_payload.get("message") or app_payload.get("message") or {}
        )

        # Search path
        candidates = [
            event.get("space"),
            chat.get("space"),
            app_payload.get("space"),
            msg_payload.get("space"),
            message.get("space"),
        ]

        for obj in candidates:
            if isinstance(obj, dict) and obj.get("name"):
                return obj["name"]

        # Recursive safety search for ANY field named 'name' starting with 'spaces/'
        def _deep_search(data: Any) -> str | None:
            if isinstance(data, dict):
                val = data.get("name")
                if isinstance(val, str) and val.startswith("spaces/"):
                    return val
                for v in data.values():
                    res = _deep_search(v)
                    if res:
                        return res
            elif isinstance(data, list):
                for item in data:
                    res = _deep_search(item)
                    if res:
                        return res
            return None

        return _deep_search(event) or "default"

    @staticmethod
    def _extract_thread_name(event: dict[str, Any]) -> str | None:
        """Robustly extract the thread resource name from any event shape."""
        chat = event.get("chat") or {}
        app_payload = chat.get("appCommandPayload") or {}
        msg_payload = chat.get("messagePayload") or {}
        message = (
            event.get("message") or msg_payload.get("message") or app_payload.get("message") or {}
        )

        # Priority 1: standard thread object.
        # Priority 2: sibling of metadata.
        candidates = [
            message.get("thread"),
            app_payload.get("thread"),
            msg_payload.get("thread"),
            event.get("thread"),
            chat.get("thread"),
        ]

        for obj in candidates:
            if isinstance(obj, dict) and obj.get("name"):
                return obj["name"]

        # Recursive search for 'thread' key
        def _search_thread(data: Any) -> dict | None:
            if isinstance(data, dict):
                if "thread" in data and isinstance(data["thread"], dict):
                    return data["thread"]
                for v in data.values():
                    res = _search_thread(v)
                    if res:
                        return res
            elif isinstance(data, list):
                for item in data:
                    res = _search_thread(item)
                    if res:
                        return res
            return None

        thread_obj = _search_thread(event)
        if thread_obj and thread_obj.get("name"):
            return thread_obj["name"]

        return None

    def _parse_message_event(self, event: dict[str, Any]) -> tuple[str, str, str, str | None]:
        """Extract ``(user_text, user_email, space_name, thread_name)``."""
        chat = event.get("chat") or {}
        msg_payload = chat.get("messagePayload") or {}
        message = event.get("message") or msg_payload.get("message") or {}

        # 1. User Text — from top-level or messagePayload.
        user_text = message.get("argumentText", "").strip()

        # 2. User Email — standard Chat path or Workspace Add-on path.
        user = event.get("user") or chat.get("user") or message.get("sender") or {}
        user_email = (user.get("email") or "unknown").lower()

        # 3. Space and Thread Names — use robust helpers.
        space_name = self._extract_space_name(event)
        thread_name = self._extract_thread_name(event)

        return user_text, user_email, space_name, thread_name

    async def _run_agent(
        self,
        *,
        session_id: str,
        user_id: str,
        user_text: str,
        user_role: str,
        space_name: str,
        thread_name: str | None,
        extra_state: dict[str, Any] | None = None,
        tracker: ProgressTracker | None = None,
    ) -> dict[str, Any]:
        """Drive a single agent turn and collect text + any buffered cards.

        When ``tracker`` is provided, each runner event is fed to it so
        the caller can render a progressive update card. The tracker
        also accumulates the response text, so this method returns the
        same-shape reply whether or not progress updates are enabled.
        """
        logger.info("Starting agent run (session_id=%s, user_id=%s)", session_id, user_id)
        # NOTE: use ``set_user_role`` rather than a raw ``user_role`` write.
        # ``GuardrailsPlugin`` runs ``ensure_default_role()`` as a
        # before_agent_callback; it resets any ``user_role`` that wasn't
        # marked server-trusted back to ``viewer`` to prevent privilege
        # escalation from untrusted session state. ``set_user_role`` sets
        # both ``user_role`` and the ``_role_set_by_server`` lock flag so
        # the callback leaves it alone.
        state_delta: dict[str, Any] = {
            "gchat_space": space_name,
            "gchat_thread": thread_name or "",
        }
        set_user_role(state_delta, user_role)
        if extra_state:
            state_delta.update(extra_state)

        message = types.Content(role="user", parts=[types.Part.from_text(text=user_text)])

        cards, token = start_request_buffer()
        try:
            response_text = ""
            async for run_event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=message,
                state_delta=state_delta,
            ):
                if tracker is not None:
                    await tracker.consume(run_event)
                else:
                    response_text += extract_reply_text(run_event)
            if tracker is not None:
                response_text = tracker.collected_text
            logger.info("Agent run complete. Collected %d characters of text.", len(response_text))
        except Exception:
            logger.exception("Agent runner failed during turn")
            raise
        finally:
            end_request_buffer(token)

        reply: dict[str, Any] = {}
        if response_text:
            reply["text"] = response_text
        if cards:
            reply["cardsV2"] = cards
        if not reply:
            reply["text"] = "(no response)"
        return reply

    async def _post_async_reply(
        self,
        *,
        space_name: str,
        thread_name: str | None,
        reply: dict[str, Any],
    ) -> None:
        """Post an agent reply via the Chat REST API."""
        if self.chat_client is None:
            logger.error("Cannot post async reply: chat_client is not configured")
            return

        if not space_name or space_name == "default":
            logger.warning("Cannot post async reply: valid space name was not found in event")
            return

        try:
            logger.info("Posting async reply to %s (thread=%s)", space_name, thread_name)
            await self.chat_client.create_message(
                space_name,
                text=reply.get("text") or None,
                cards_v2=reply.get("cardsV2"),
                thread_name=thread_name,
            )
            logger.info("Successfully posted async reply")
        except Exception:
            logger.exception("Failed to post async reply to %s", space_name)

    async def _post_async_error(
        self,
        space_name: str | None,
        thread_name: str | None,
        *,
        message_name: str | None = None,
    ) -> None:
        """Best-effort error notification when a background run crashes.

        If a progress card is already showing (``message_name``), replace
        it in place with an error card so the user doesn't see a stuck
        "Investigating…" frame.
        """
        if self.chat_client is None or not space_name:
            return
        error_text = "Sorry, I hit an unexpected error. Please try again."
        try:
            if message_name is not None:
                result = await self.chat_client.update_message(
                    message_name,
                    cards_v2=[build_error_card(error_text)],
                )
                if result is not None:
                    return
            await self.chat_client.create_message(
                space_name,
                text=error_text,
                thread_name=thread_name,
            )
        except Exception:
            logger.exception("Failed to post async error notification")

    # ── MESSAGE ───────────────────────────────────────────────────────

    async def _handle_message(self, event: dict[str, Any]) -> dict[str, Any]:
        user_text, user_email, space_name, thread_name = self._parse_message_event(event)

        if not user_text:
            return self._wrap_for_addons("How can I help you today?")

        session_id = f"gchat:{thread_name or space_name}"

        result = await self._run_agent(
            session_id=session_id,
            user_id=user_email,
            user_text=user_text,
            user_role=self.resolve_role(user_email),
            space_name=space_name,
            thread_name=thread_name,
        )

        return self._wrap_for_addons(result.get("text", "(no response)"), result.get("cardsV2"))

    async def _handle_message_async(self, event: dict[str, Any]) -> None:
        """Background-task counterpart to ``_handle_message``."""
        logger.info("Background task started for MESSAGE event")
        user_text, user_email, space_name, thread_name = self._parse_message_event(event)
        logger.info(
            "Parsed: user_text='%s', user_email='%s', space_name='%s', thread_name='%s'",
            user_text,
            user_email,
            space_name,
            thread_name,
        )
        progress_message_name: str | None = None
        try:
            if not user_text:
                await self._post_async_reply(
                    space_name=space_name,
                    thread_name=thread_name,
                    reply={"text": "How can I help you today?"},
                )
                return

            session_id = f"gchat:{thread_name or space_name}"
            user_role = self.resolve_role(user_email)

            # 1. Post the initial "Investigating…" progress card. We
            #    keep its resource name so subsequent PATCHes update the
            #    same message in place instead of spamming the thread.
            progress_message_name = await self._post_initial_progress(
                space_name=space_name, thread_name=thread_name
            )
            tracker = self._make_tracker(progress_message_name)

            try:
                result = await self._run_agent(
                    session_id=session_id,
                    user_id=user_email,
                    user_text=user_text,
                    user_role=user_role,
                    space_name=space_name,
                    thread_name=thread_name,
                    tracker=tracker,
                )
            finally:
                # Flush one last progress frame so the user never sees a
                # stale card if the run finishes between debounce ticks.
                if tracker is not None:
                    await tracker.flush_final()

            await self._post_final_result(
                space_name=space_name,
                thread_name=thread_name,
                progress_message_name=progress_message_name,
                tracker=tracker,
                reply=result,
                user_role=user_role,
            )
        except Exception:
            logger.exception("Async message processing failed")
            await self._post_async_error(
                space_name, thread_name, message_name=progress_message_name
            )

    async def _post_initial_progress(
        self, *, space_name: str, thread_name: str | None
    ) -> str | None:
        """Post the initial progress card and return its resource name.

        Returns ``None`` when the Chat client is unavailable or posting
        fails — callers in that case fall back to the single-post path.
        """
        if self.chat_client is None or not space_name or space_name == "default":
            return None
        try:
            card = build_progress_card(
                current_agent=None,
                current_tool=None,
                subsystem_chips={},
                remediation=None,
                elapsed_seconds=0.0,
            )
            response = await self.chat_client.create_message(
                space_name,
                cards_v2=[card],
                thread_name=thread_name,
            )
            name = response.get("name") if isinstance(response, dict) else None
            if not name:
                logger.warning("Chat API create_message returned no message name")
            return name
        except Exception:
            logger.exception("Failed to post initial progress card to %s", space_name)
            return None

    def _make_tracker(self, message_name: str | None) -> ProgressTracker | None:
        """Build a tracker that PATCHes ``message_name宣 on each update."""
        if self.chat_client is None or not message_name:
            return None

        chat_client = self.chat_client

        async def on_update(t: ProgressTracker) -> None:
            card = build_progress_card(
                current_agent=t.current_agent,
                current_tool=t.current_tool,
                subsystem_chips=t.subsystem_chips,
                remediation=t.remediation_state or None,
                elapsed_seconds=t.elapsed_seconds,
            )
            await chat_client.update_message(message_name, cards_v2=[card])

        return ProgressTracker(on_update=on_update)

    async def _post_final_result(
        self,
        *,
        space_name: str,
        thread_name: str | None,
        progress_message_name: str | None,
        tracker: ProgressTracker | None,
        reply: dict[str, Any],
        user_role: str,
    ) -> None:
        """Replace the progress card with the final result, or post fresh.

        If any subsystem chip landed during the run we render a
        structured triage result card. Otherwise (a targeted query like
        "what's the kafka lag?") we fall back to the reply's text + any
        buffered confirmation cards.
        """
        has_triage_data = tracker is not None and (tracker.subsystem_chips or tracker.triage_report)

        if has_triage_data and tracker is not None:
            triage_card = build_triage_result_card(
                subsystem_chips=tracker.subsystem_chips,
                triage_report=tracker.triage_report or reply.get("text"),
                user_role=user_role,
            )
            final_cards: list[dict[str, Any]] = [triage_card]
            # Any buffered confirmation cards from guarded tools must
            # still reach the user so they can approve/deny them.
            if reply.get("cardsV2"):
                final_cards.extend(reply["cardsV2"])
            await self._update_or_post(
                space_name=space_name,
                thread_name=thread_name,
                message_name=progress_message_name,
                reply={"cardsV2": final_cards},
            )
            return

        # Non-triage path: keep the original text+cards reply.
        await self._update_or_post(
            space_name=space_name,
            thread_name=thread_name,
            message_name=progress_message_name,
            reply=reply,
        )

    async def _update_or_post(
        self,
        *,
        space_name: str,
        thread_name: str | None,
        message_name: str | None,
        reply: dict[str, Any],
    ) -> None:
        """Update the progress message in place, falling back to a new post.

        When replacing a progress card with a plain-text final reply, we
        must explicitly send ``cards_v2=[]`` so the Chat API clears the
        previously-posted "Investigating…" card. Chat preserves any
        field not listed in ``updateMask``, so omitting ``cardsV2`` would
        leave the progress card rendered next to the new text.
        """
        if self.chat_client is None:
            logger.error("Cannot post final reply: chat_client is not configured")
            return

        text = reply.get("text")
        cards_v2 = reply.get("cardsV2")
        if not text and not cards_v2:
            text = "(no response)"

        if message_name is not None:
            try:
                result = await self.chat_client.update_message(
                    message_name,
                    text=text if text is not None else "",
                    cards_v2=cards_v2 if cards_v2 is not None else [],
                )
                if result is not None:
                    return
                logger.info("Progress message gone; posting final reply as a new message")
            except Exception:
                logger.exception("Failed to update progress card; falling back to a new message")

        await self._post_async_reply(
            space_name=space_name,
            thread_name=thread_name,
            reply={"text": text, "cardsV2": cards_v2} if cards_v2 else {"text": text},
        )

    # ── APP_COMMAND ───────────────────────────────────────────────────

    def _resolve_pending_for_click(self, method: str, key: str) -> tuple[Any, str | None]:
        """Look up the pending entry for a click and route by ``method``.

        Returns ``(pending, error_msg)``. For the Approve path we mark
        the latest matching pending as approved (so the callback can
        consume it on the LLM retry); for Deny we just pop it. ``error_msg``
        is non-None when no pending matched and the caller should surface
        that text to the user.
        """
        if method == "confirm_action":
            pending = self.store.mark_latest_approved_for_thread(key)
        elif method == "deny_action":
            pending = self.store.pop_latest_for_thread(key)
        else:
            return None, f"Unknown action: {method}"
        if pending is None:
            return None, "No pending action found in this thread."
        return pending, None

    async def _handle_app_command(self, event: dict[str, Any]) -> dict[str, Any]:
        """Process a Quick Command (Approve/Deny) synchronously."""
        chat = event.get("chat") or {}
        payload = chat.get("appCommandPayload") or {}
        metadata = payload.get("appCommandMetadata") or {}
        command_id = metadata.get("appCommandId")

        # 1. Resolve identity, space and thread
        user = chat.get("user") or {}
        user_email = (user.get("email") or "unknown").lower()
        space_name = self._extract_space_name(event)
        thread_name = self._extract_thread_name(event)

        # 2. Map command ID to method
        method = None
        if command_id == 1:
            method = "confirm_action"
        elif command_id == 2:
            method = "deny_action"

        if not method:
            return self._wrap_for_addons("Unknown command ID.")

        # 3. Resolve the matching pending entry. Quick-command payloads
        #    don't carry a thread, so we fall back to the space — which
        #    can match the wrong pending if multiple destructive actions
        #    are awaiting approval in the same shared space.
        key = thread_name or space_name
        if not thread_name:
            logger.warning(
                "APP_COMMAND has no thread name; matching latest pending in space=%s",
                space_name,
            )
        pending, error = self._resolve_pending_for_click(method, key)
        if pending is None:
            return self._wrap_for_addons(error or "No pending action found in this thread.")

        display_name = user.get("displayName") or user_email
        synthetic = self._build_click_synthetic(pending, method, display_name)
        if synthetic is None:
            return self._wrap_for_addons(f"Unknown action: {method}")

        synthetic_text, ack_text = synthetic

        result = await self._run_agent(
            session_id=pending.session_id,
            user_id=pending.user_id,
            user_text=synthetic_text,
            user_role=self.resolve_role(pending.user_id),
            space_name=pending.space_name,
            thread_name=pending.thread_name,
        )

        combined_text = ack_text
        if result.get("text"):
            combined_text = f"{ack_text}\n\n{result['text']}"

        return self._wrap_for_addons(combined_text, result.get("cardsV2"))

    async def _handle_app_command_async(self, event: dict[str, Any]) -> None:
        """Background-task counterpart to ``_handle_app_command``."""
        chat = event.get("chat") or {}
        payload = chat.get("appCommandPayload") or {}
        metadata = payload.get("appCommandMetadata") or {}
        command_id = metadata.get("appCommandId")

        user = chat.get("user") or {}
        user_email = (user.get("email") or "unknown").lower()
        space_name = self._extract_space_name(event)
        thread_name = self._extract_thread_name(event)

        method = None
        if command_id == 1:
            method = "confirm_action"
        elif command_id == 2:
            method = "deny_action"

        if not method:
            return

        key = thread_name or space_name
        if not thread_name:
            logger.warning(
                "APP_COMMAND has no thread name; matching latest pending in space=%s",
                space_name,
            )
        pending, error = self._resolve_pending_for_click(method, key)
        if pending is None:
            await self._post_async_reply(
                space_name=space_name,
                thread_name=thread_name,
                reply={"text": error or "No pending action found in this thread."},
            )
            return

        display_name = user.get("displayName") or user_email
        synthetic = self._build_click_synthetic(pending, method, display_name)
        if synthetic is None:
            return
        synthetic_text, ack_text = synthetic

        progress_message_name: str | None = None
        try:
            # Show an in-thread progress card so the user has feedback
            # during the post-approve LLM run instead of staring at a
            # silent thread for ~30 seconds.
            progress_message_name = await self._post_initial_progress(
                space_name=pending.space_name, thread_name=pending.thread_name
            )
            tracker = self._make_tracker(progress_message_name)

            try:
                result = await self._run_agent(
                    session_id=pending.session_id,
                    user_id=pending.user_id,
                    user_text=synthetic_text,
                    user_role=self.resolve_role(pending.user_id),
                    space_name=pending.space_name,
                    thread_name=pending.thread_name,
                    tracker=tracker,
                )
            finally:
                if tracker is not None:
                    await tracker.flush_final()

            combined_text = ack_text
            if result.get("text"):
                combined_text = f"{ack_text}\n\n{result['text']}"

            await self._update_or_post(
                space_name=pending.space_name,
                thread_name=pending.thread_name,
                message_name=progress_message_name,
                reply={"text": combined_text, "cardsV2": result.get("cardsV2")},
            )
        except Exception:
            logger.exception("Async app command processing failed")
            await self._post_async_error(
                pending.space_name, pending.thread_name, message_name=progress_message_name
            )

    # ── CARD_CLICKED ──────────────────────────────────────────────────

    def _parse_card_click_event(self, event: dict[str, Any]) -> tuple[str | None, str | None, str]:
        """Return ``(action_id, method, display_name)`` from a click event."""
        common = event.get("common") or event.get("commonEventObject") or {}
        action = event.get("action") or {}

        params = common.get("parameters") or action.get("parameters") or []
        if isinstance(params, list):
            params = {p.get("key"): p.get("value") for p in params if isinstance(p, dict)}

        method = common.get("invokedFunction") or action.get("actionMethodName")
        action_id = params.get("action_id") if isinstance(params, dict) else None

        chat = event.get("chat") or {}
        user = event.get("user") or chat.get("user") or {}
        display_name = user.get("displayName") or user.get("email") or "unknown"

        return action_id, method, display_name

    def _build_click_synthetic(
        self, pending: Any, method: str, display_name: str
    ) -> tuple[str, str] | None:
        """Derive ``(synthetic_text, ack_text)`` for a click.

        The synthetic prompt for Approve embeds the original arguments
        verbatim so the LLM doesn't have to reconstruct them from chat
        history — when the call is made from a sub-agent, the parent
        runner only sees the sub-agent's request/response, not the inner
        tool args. Returns ``None`` if the method is unrecognized.
        """
        if method == "confirm_action":
            args = getattr(pending, "args", None) or {}
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
            args_clause = f" with arguments {args_str}" if args_str else ""
            return (
                (
                    f"The operator ({display_name}) approved the previous "
                    f"`{pending.tool_name}` request. Re-issue the same call "
                    f"now{args_clause} and report the result."
                ),
                f"*Approved* by {display_name} — executing `{pending.tool_name}`",
            )
        if method == "deny_action":
            return (
                (
                    f"The operator ({display_name}) denied the previous "
                    f"`{pending.tool_name}` request. Do not retry it; "
                    f"acknowledge that the action was cancelled."
                ),
                f"*Denied* by {display_name} — `{pending.tool_name}` was not executed.",
            )
        return None

    def _resolve_card_click_pending(self, action_id: str, method: str) -> tuple[Any, str | None]:
        """Look up + route the pending entry for a CARD_CLICKED event.

        Mirrors :meth:`_resolve_pending_for_click` for the legacy button
        path: Approve marks the entry approved (so the callback consumes
        it on retry); Deny pops it.
        """
        if method == "confirm_action":
            pending = self.store.get(action_id)
            if pending is None:
                return None, "This action has expired or was already processed."
            pending.approved = True
            pending.approved_at = time.time()
            return pending, None
        if method == "deny_action":
            pending = self.store.pop(action_id)
            if pending is None:
                return None, "This action has expired or was already processed."
            return pending, None
        return None, f"Unknown action: {method}"

    async def _handle_card_click(self, event: dict[str, Any]) -> dict[str, Any]:
        """Handle Approve/Deny/Run-Remediation button clicks."""
        action_id, method, display_name = self._parse_card_click_event(event)

        # Run-Remediation is a standalone action — no pending-confirmation
        # lookup needed because it just dispatches a new agent turn.
        if method == "run_remediation":
            return await self._handle_run_remediation_sync(event, display_name)

        if not action_id or not method:
            logger.warning("CARD_CLICKED missing action_id or method")
            return self._wrap_for_addons("This card action is not recognized.")

        pending, error = self._resolve_card_click_pending(action_id, method)
        if pending is None:
            return self._wrap_for_addons(
                error or "This action has expired or was already processed."
            )

        synthetic = self._build_click_synthetic(pending, method, display_name)
        if synthetic is None:
            return self._wrap_for_addons(f"Unknown action: {method}")
        synthetic_text, ack_text = synthetic

        result = await self._run_agent(
            session_id=pending.session_id,
            user_id=pending.user_id,
            user_text=synthetic_text,
            user_role=self.resolve_role(pending.user_id),
            space_name=pending.space_name,
            thread_name=pending.thread_name,
        )

        combined_text = ack_text
        if result.get("text"):
            combined_text = f"{ack_text}\n\n{result['text']}"

        return self._wrap_for_addons(combined_text, result.get("cardsV2"))

    async def _handle_card_click_async(self, event: dict[str, Any]) -> None:
        """Background-task counterpart to ``_handle_card_click``."""
        action_id, method, display_name = self._parse_card_click_event(event)

        # Run-Remediation bypasses the pending-confirmation lookup.
        if method == "run_remediation":
            await self._handle_run_remediation_async(event, display_name)
            return

        if not action_id or not method:
            logger.warning("CARD_CLICKED missing action_id or method")
            # We don't know where to post, so just drop it. The top-level
            # handler returned an ack already, so the UI is consistent.
            return

        pending, error = self._resolve_card_click_pending(action_id, method)
        if pending is None:
            space_name = self._extract_space_name(event)
            if space_name:
                await self._post_async_reply(
                    space_name=space_name,
                    thread_name=self._extract_thread_name(event),
                    reply={"text": error or "This action has expired or was already processed."},
                )
            return

        synthetic = self._build_click_synthetic(pending, method, display_name)
        if synthetic is None:
            await self._post_async_reply(
                space_name=pending.space_name,
                thread_name=pending.thread_name,
                reply={"text": f"Unknown action: {method}"},
            )
            return
        synthetic_text, ack_text = synthetic

        progress_message_name: str | None = None
        try:
            progress_message_name = await self._post_initial_progress(
                space_name=pending.space_name, thread_name=pending.thread_name
            )
            tracker = self._make_tracker(progress_message_name)

            try:
                result = await self._run_agent(
                    session_id=pending.session_id,
                    user_id=pending.user_id,
                    user_text=synthetic_text,
                    user_role=self.resolve_role(pending.user_id),
                    space_name=pending.space_name,
                    thread_name=pending.thread_name,
                    tracker=tracker,
                )
            finally:
                if tracker is not None:
                    await tracker.flush_final()

            combined_text = ack_text
            if result.get("text"):
                combined_text = f"{ack_text}\n\n{result['text']}"

            await self._update_or_post(
                space_name=pending.space_name,
                thread_name=pending.thread_name,
                message_name=progress_message_name,
                reply={"text": combined_text, "cardsV2": result.get("cardsV2")},
            )
        except Exception:
            logger.exception("Async card click processing failed")
            await self._post_async_error(
                pending.space_name, pending.thread_name, message_name=progress_message_name
            )

    # ── Run Remediation click ────────────────────────────────────────

    _REMEDIATION_PROMPT = (
        "Remediate the current incident. Use the triage report in session state "
        "to decide which action to take using k8s_health_agent (restart, scale, or rollback). "
        "Report the outcome."
    )

    def _click_user_email(self, event: dict[str, Any]) -> str:
        chat = event.get("chat") or {}
        user = event.get("user") or chat.get("user") or {}
        return (user.get("email") or "unknown").lower()

    async def _handle_run_remediation_sync(
        self, event: dict[str, Any], display_name: str
    ) -> dict[str, Any]:
        """Sync-path dispatch for Run-Remediation clicks."""
        space_name = self._extract_space_name(event)
        thread_name = self._extract_thread_name(event)
        user_email = self._click_user_email(event)
        user_role = self.resolve_role(user_email)
        session_id = f"gchat:{thread_name or space_name}"

        ack_text = f"*Remediation requested* by {display_name} — running pipeline…"
        result = await self._run_agent(
            session_id=session_id,
            user_id=user_email,
            user_text=self._REMEDIATION_PROMPT,
            user_role=user_role,
            space_name=space_name,
            thread_name=thread_name,
        )
        combined_text = ack_text
        if result.get("text"):
            combined_text = f"{ack_text}\n\n{result['text']}"
        return self._wrap_for_addons(combined_text, result.get("cardsV2"))

    async def _handle_run_remediation_async(self, event: dict[str, Any], display_name: str) -> None:
        """Async-path dispatch for Run-Remediation clicks.

        Posts its own progress card in the thread — intentionally
        separate from any prior triage card so operators keep both the
        "what's wrong" and "what we're doing about it" context side by
        side.
        """
        space_name = self._extract_space_name(event)
        thread_name = self._extract_thread_name(event)
        user_email = self._click_user_email(event)
        user_role = self.resolve_role(user_email)
        session_id = f"gchat:{thread_name or space_name}"

        progress_message_name: str | None = None
        try:
            progress_message_name = await self._post_initial_progress(
                space_name=space_name, thread_name=thread_name
            )
            tracker = self._make_tracker(progress_message_name)

            try:
                result = await self._run_agent(
                    session_id=session_id,
                    user_id=user_email,
                    user_text=self._REMEDIATION_PROMPT,
                    user_role=user_role,
                    space_name=space_name,
                    thread_name=thread_name,
                    tracker=tracker,
                )
            finally:
                if tracker is not None:
                    await tracker.flush_final()

            ack_prefix = f"*Remediation requested* by {display_name} — pipeline complete."
            final_text = f"{ack_prefix}\n\n{result['text']}" if result.get("text") else ack_prefix
            reply_with_ack: dict[str, Any] = {"text": final_text}
            if result.get("cardsV2"):
                reply_with_ack["cardsV2"] = result["cardsV2"]
            await self._update_or_post(
                space_name=space_name,
                thread_name=thread_name,
                message_name=progress_message_name,
                reply=reply_with_ack,
            )
        except Exception:
            logger.exception("Async run_remediation processing failed")
            await self._post_async_error(
                space_name, thread_name, message_name=progress_message_name
            )
