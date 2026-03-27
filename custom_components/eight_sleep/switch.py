from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from custom_components.eight_sleep.pyEight.user import EightUser

from .pyEight.eight import EightSleep

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from . import EightSleepBaseEntity, EightSleepConfigEntryData
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _format_weekdays(repeat: dict) -> str | list[str]:
    """Convert the new repeat.weekDays dict to a display-friendly format."""
    if not repeat.get("enabled", False):
        return "Once"
    weekdays = repeat.get("weekDays", {})
    day_names = [day.capitalize() for day, active in weekdays.items() if active]
    if len(day_names) == 7:
        return "Every day"
    if not day_names:
        return "Once"
    return day_names


def _make_alarm_key(alarm_id: str) -> str:
    """Build a stable entity key from an alarm's UUID."""
    return f"alarm_{alarm_id}"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    config_entry_data: EightSleepConfigEntryData = hass.data[DOMAIN][entry.entry_id]
    eight = config_entry_data.api
    coordinator = config_entry_data.user_coordinator

    # Track entities per user keyed by alarm_id for dynamic add/remove
    tracked: dict[str, dict[str, EightSwitchEntity]] = {}

    for user in eight.users.values():
        user_entities: dict[str, EightSwitchEntity] = {}
        for index, alarm in enumerate(user.alarms, start=1):
            entity = _create_alarm_entity(entry, coordinator, eight, user, alarm, index)
            user_entities[alarm["id"]] = entity
        tracked[user.user_id] = user_entities

    # Add all initial entities (alarm switches + skip-alarm-if-empty per user)
    all_entities: list[SwitchEntity] = [e for user_map in tracked.values() for e in user_map.values()]
    for user in eight.users.values():
        all_entities.append(
            EightSkipAlarmIfEmptySwitch(entry, coordinator, eight, user)
        )
    async_add_entities(all_entities)

    # Register a listener to dynamically add/remove entities on coordinator refresh
    @callback
    def _async_sync_alarm_entities() -> None:
        ent_reg = er.async_get(hass)

        for user in eight.users.values():
            user_entities = tracked.setdefault(user.user_id, {})
            current_ids = {alarm["id"] for alarm in user.alarms}
            tracked_ids = set(user_entities.keys())

            # Add entities for new alarms
            new_ids = current_ids - tracked_ids
            if new_ids:
                next_index = len(user_entities) + 1
                new_entities = []
                for alarm in user.alarms:
                    if alarm["id"] in new_ids:
                        entity = _create_alarm_entity(
                            entry, coordinator, eight, user, alarm, next_index
                        )
                        next_index += 1
                        user_entities[alarm["id"]] = entity
                        new_entities.append(entity)
                async_add_entities(new_entities)
                _LOGGER.debug("Added %d new alarm entities for user %s", len(new_entities), user.user_id)

            # Remove entities for deleted alarms
            removed_ids = tracked_ids - current_ids
            for alarm_id in removed_ids:
                entity = user_entities.pop(alarm_id)
                entity_id = ent_reg.async_get_entity_id(
                    "switch", DOMAIN, entity.unique_id
                )
                if entity_id:
                    ent_reg.async_remove(entity_id)
                    _LOGGER.debug("Removed alarm entity %s for user %s", entity_id, user.user_id)

    coordinator.async_add_listener(_async_sync_alarm_entities)


def _create_alarm_entity(
    entry: ConfigEntry,
    coordinator: DataUpdateCoordinator,
    eight: EightSleep,
    user: EightUser,
    alarm: dict[str, Any],
    index: int,
) -> EightSwitchEntity:
    """Create a switch entity for a single alarm."""
    alarm_id = alarm["id"]
    description = SwitchEntityDescription(
        key=_make_alarm_key(alarm_id),
        name=f"Alarm {index}",
        icon="mdi:alarm",
    )
    return EightSwitchEntity(entry, coordinator, eight, user, description, alarm_id)


class EightSwitchEntity(EightSleepBaseEntity, SwitchEntity):
    """Representation of an Eight Sleep switch entity."""

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: DataUpdateCoordinator,
        eight: EightSleep,
        user: EightUser,
        entity_description: SwitchEntityDescription,
        alarm_id: str,
    ) -> None:
        super().__init__(entry, coordinator, eight, user, entity_description.key)
        self.entity_description = entity_description
        self._alarm_id = alarm_id
        # Set clean positional name for entity_id generation at registration
        self._attr_name = entity_description.name
        self._attr_extra_state_attributes = {}
        self._update_attributes()

    def _update_attributes(self) -> None:
        if self._user_obj:
            self._attr_is_on = self._user_obj.get_alarm_enabled(self._alarm_id)

            for alarm in self._user_obj.alarms:
                if alarm["id"] == self._alarm_id:
                    self._attr_extra_state_attributes["time"] = alarm.get("time")
                    self._attr_extra_state_attributes["days"] = _format_weekdays(
                        alarm.get("repeat", {})
                    )
                    self._attr_extra_state_attributes["thermal"] = alarm.get("thermal", {})
                    self._attr_extra_state_attributes["vibration"] = alarm.get("vibration", {})
                    self._attr_extra_state_attributes["snoozing"] = alarm.get("snoozing", False)
                    self._attr_extra_state_attributes["snoozed_until"] = alarm.get("snoozedUntil")
                    return

        self._attr_extra_state_attributes.pop("time", None)
        self._attr_extra_state_attributes.pop("days", None)
        self._attr_extra_state_attributes.pop("thermal", None)
        self._attr_extra_state_attributes.pop("vibration", None)
        self._attr_extra_state_attributes.pop("snoozing", None)
        self._attr_extra_state_attributes.pop("snoozed_until", None)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._user_obj:
            await self._user_obj.set_alarm_enabled(None, self._alarm_id, True)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._user_obj:
            await self._user_obj.set_alarm_enabled(None, self._alarm_id, False)
            await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_attributes()
        # Update display name dynamically (entity_id is already locked at registration)
        if self._user_obj:
            for alarm in self._user_obj.alarms:
                if alarm["id"] == self._alarm_id:
                    alarm_time = alarm.get("time", "")
                    if alarm_time:
                        self._attr_name = f"Alarm {alarm_time[:5]}"
                    break
        super()._handle_coordinator_update()


_EMPTY_READINGS_REQUIRED = 2  # Consecutive empty readings before dismissing
_PRE_ALARM_WINDOW_SECONDS = 300  # Start checking 5 minutes before alarm
_POST_ALARM_WINDOW_SECONDS = 300  # Keep trying to dismiss 5 minutes after alarm

# Start attempting dismiss 10s before nextTimestamp. Eight Sleep's backend
# often accepts dismiss slightly before the alarm's nominal ring time, so
# firing a few seconds early catches the "now ringing" transition faster.
_DISMISS_LEAD_SECONDS = 10

# Tight retry loop inside _dismiss_alarm. The API may 409 ("not ringing yet")
# for several seconds after nextTimestamp; without this, we'd wait a full
# 30s coordinator poll between attempts. 15s interval is a deliberate
# middle ground: fast enough to catch the ring window, slow enough to
# avoid hammering the Eight Sleep API. Capped at 90s so a stuck task
# can't run forever — coordinator-driven retry takes over after that.
_DISMISS_RETRY_INTERVAL = 15
_DISMISS_RETRY_MAX_SECONDS = 90


class EightSkipAlarmIfEmptySwitch(EightSleepBaseEntity, SwitchEntity, RestoreEntity):
    """Switch that auto-dismisses an alarm when the user's side has no presence.

    When enabled, starts checking bed presence 5 minutes before the alarm
    fires. If the side is unoccupied for 2 consecutive coordinator updates
    (~60s), the alarm is dismissed before it rings. Prevents alarms from
    waking the other partner when one side is empty.

    ──────────────────────────────────────────────────────────────────────
    Logging conventions (safe to ship upstream):

      WARNING : genuine problems — unexpected exceptions, mid-window
                alarm_id flips that aren't post-dismiss transitions.
      INFO    : lifecycle events — window open/close, dismiss attempted,
                dismiss succeeded. A few entries per day. Safe to leave
                enabled long-term for visibility.
      DEBUG   : per-poll heartbeat + 409 retries. Noisy; enable only
                when investigating a specific alarm cycle.

    Recommended HA configuration.yaml snippets:

      Quiet / lifecycle only (a few lines per day):
          logger:
            logs:
              custom_components.eight_sleep.switch: info

      Full diagnostic (heartbeat every ~30s):
          logger:
            logs:
              custom_components.eight_sleep.switch: debug
    ──────────────────────────────────────────────────────────────────────
    """

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: DataUpdateCoordinator,
        eight: EightSleep,
        user: EightUser,
    ) -> None:
        super().__init__(entry, coordinator, eight, user, "skip_alarm_if_empty")
        self._attr_name = "Skip Alarm If Empty"
        self._attr_icon = "mdi:bed-empty"
        self._attr_is_on = False
        self._empty_count: int = 0
        self._dismissed_alarm_id: str | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}
        self._last_approaching: bool = False
        self._last_next_alarm_id: str | None = None
        # Prevents multiple _dismiss_alarm tasks from running concurrently.
        # The tight retry loop can run up to 90s, during which coordinator
        # polls would otherwise spawn redundant dismiss tasks.
        self._dismiss_in_flight: bool = False
        # Per-window diagnostic counters. Reset on window OPEN, summarized
        # in the window CLOSED log line so any failure mode is visible
        # from INFO-level logs without needing DEBUG heartbeats.
        self._window_max_empty_count: int = 0
        self._window_dismiss_attempted: bool = False
        self._window_dismiss_succeeded: bool = False

    def _who(self) -> str:
        """Return a stable log identifier: firstName(user_id prefix)."""
        if not self._user_obj:
            return "unknown"
        name = "unknown"
        if self._user_obj.user_profile:
            name = self._user_obj.user_profile.get("firstName", "unknown")
        return f"{name}({self._user_obj.user_id[:8]})"

    def _presence_reason(self) -> str:
        """Explain WHY bed_presence returned its current value.

        Mirrors the decision tree in EightUser.bed_presence so we can log
        the reasoning in the heartbeat without modifying that property.
        Read-only; safe to call from any sync context.

        Returns a short string tag: fresh_hr / stale_hr_fallback /
        absent / no_hr_data / no_user_obj, followed by the raw values
        (last HR timestamp, age, bed_state_type) so we can tell a false
        positive from a real presence at a glance.
        """
        if not self._user_obj:
            return "no_user_obj"
        # pylint: disable=protected-access
        timeseries = self._user_obj._trend_timeseries()
        if not timeseries or "heartRate" not in timeseries:
            return (
                f"no_hr_data(last_known={self._user_obj._last_known_presence})"
            )
        hr_entry = timeseries["heartRate"][-1]
        hr_ts = hr_entry[0]
        try:
            hr_time = datetime.fromisoformat(hr_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return f"bad_hr_ts({hr_ts!r})"
        age = (datetime.now(timezone.utc) - hr_time).total_seconds()
        state = self._user_obj.bed_state_type
        if age < 600:
            return f"fresh_hr(ts={hr_ts}, age={age:.0f}s)"
        if age < 1800 and state and state.startswith("smart:"):
            return (
                f"stale_hr_fallback(ts={hr_ts}, age={age:.0f}s, state={state})"
            )
        return f"absent(ts={hr_ts}, age={age:.0f}s, state={state})"

    async def async_added_to_hass(self) -> None:
        """Restore on/off state after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        restored_from = last_state.state if last_state else "none"
        if last_state and last_state.state == "on":
            self._attr_is_on = True
        _LOGGER.debug(
            "Skip alarm: loaded for %s (enabled=%s, restored_from=%s)",
            self._who(),
            self._attr_is_on,
            restored_from,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.info("Skip alarm: turned ON for %s", self._who())
        self._attr_is_on = True
        self._empty_count = 0
        self._dismissed_alarm_id = None
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.info("Skip alarm: turned OFF for %s", self._who())
        self._attr_is_on = False
        self._empty_count = 0
        self._dismissed_alarm_id = None
        self._last_approaching = False
        self._window_max_empty_count = 0
        self._window_dismiss_attempted = False
        self._window_dismiss_succeeded = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        # Heartbeat runs on every coordinator tick while the switch is on,
        # regardless of whether the alarm window is open. The ABSENCE of a
        # heartbeat in the logs is itself a diagnostic signal: it means the
        # coordinator listener did not fire (e.g. cascade failure).
        if self._attr_is_on:
            self._check_skip_alarm()
        super()._handle_coordinator_update()

    def _is_alarm_approaching(self) -> bool:
        """Check if the next alarm is within the check window.

        Uses a pure time-based window around next_alarm (the actual alarm
        time, NOT the thermal pre-warm startTimestamp). The window spans
        from 5 min before the alarm to 5 min after, giving us retries in
        case the first dismiss attempt hits a 409 (alarm not ringing yet).
        """
        if not self._user_obj or not self._user_obj.next_alarm_id or not self._user_obj.next_alarm:
            return False
        now = datetime.now(timezone.utc)
        time_until = (self._user_obj.next_alarm - now).total_seconds()
        return -_POST_ALARM_WINDOW_SECONDS <= time_until <= _PRE_ALARM_WINDOW_SECONDS

    def _log_heartbeat(
        self,
        approaching: bool,
        bed_occupied: bool | None,
        time_until: float | None,
    ) -> None:
        """Per-poll state line — the primary diagnostic tool.

        Two flavours to keep debug logs readable:

        * **Compact** (single short line) when nothing interesting is
          happening — out of the alarm window, empty_count is zero, no
          active dismiss tracking. This preserves the liveness signal
          (absence of heartbeats = listener not firing) without spamming.
        * **Full** (all diagnostic fields) when we are in the alarm
          window or any state variable is non-default. This is when we
          actually need every variable to diagnose a failure.
        """
        if not self._user_obj:
            _LOGGER.debug("Skip alarm heartbeat: no user_obj for %s", self._who())
            return
        next_alarm = self._user_obj.next_alarm

        interesting = (
            approaching
            or self._empty_count > 0
            or self._dismissed_alarm_id is not None
        )
        if not interesting:
            _LOGGER.debug(
                "Skip alarm heartbeat for %s: idle, next_alarm=%s, time_until=%s",
                self._who(),
                next_alarm.isoformat() if next_alarm else "none",
                f"{time_until:.0f}s" if time_until is not None else "none",
            )
            return

        _LOGGER.debug(
            "Skip alarm heartbeat for %s: on=%s, in_window=%s, "
            "next_alarm_id=%s, next_alarm=%s, time_until=%s, "
            "bed_occupied=%s [%s], empty_count=%d/%d, "
            "dismissed_alarm_id=%s",
            self._who(),
            self._attr_is_on,
            approaching,
            self._user_obj.next_alarm_id,
            next_alarm.isoformat() if next_alarm else "none",
            f"{time_until:.0f}s" if time_until is not None else "none",
            bed_occupied,
            self._presence_reason(),
            self._empty_count,
            _EMPTY_READINGS_REQUIRED,
            self._dismissed_alarm_id,
        )

    def _check_skip_alarm(self) -> None:
        if not self._user_obj:
            _LOGGER.debug("Skip alarm: no user_obj for %s, skipping", self._who())
            return

        current_alarm_id = self._user_obj.next_alarm_id
        next_alarm = self._user_obj.next_alarm
        now = datetime.now(timezone.utc)
        time_until = (next_alarm - now).total_seconds() if next_alarm else None
        approaching = self._is_alarm_approaching()
        bed_occupied = self._user_obj.bed_presence

        # Heartbeat first — we always want visibility into the current state
        # even if we early-return below. Its presence/absence in the logs is
        # the canonical signal for "is the listener firing at all".
        self._log_heartbeat(approaching, bed_occupied, time_until)

        # Detect mid-window alarm_id flips — should not happen after the
        # endTimestamp fix in update_routines_data, but worth a WARNING if
        # it ever does because it means the dismiss target is moving.
        #
        # Suppress the expected post-dismiss transition: once we dismiss an
        # alarm, the API flips next_alarm to tomorrow's (or the next one),
        # which shows up as an alarm_id change AND pushes us out of the
        # window. Both conditions must be true to call it a real mid-window
        # flip: still approaching AND the old id is not the one we just
        # dismissed.
        if (
            self._last_next_alarm_id is not None
            and current_alarm_id is not None
            and self._last_next_alarm_id != current_alarm_id
            and self._last_approaching
            and approaching
            and self._dismissed_alarm_id != self._last_next_alarm_id
        ):
            _LOGGER.warning(
                "Skip alarm: next_alarm_id changed mid-window for %s: %s -> %s",
                self._who(),
                self._last_next_alarm_id,
                current_alarm_id,
            )
        self._last_next_alarm_id = current_alarm_id

        # Window transitions — INFO level. On OPEN we reset per-window
        # tracking so the CLOSED log can summarize the full cycle. On
        # CLOSED we emit a one-line outcome including peak empty_count,
        # whether dismiss was tried, whether it succeeded, and the final
        # presence reasoning. This single log entry answers "why did the
        # alarm (or didn't) get dismissed this cycle" without needing
        # DEBUG heartbeats.
        if approaching != self._last_approaching:
            if approaching:
                self._window_max_empty_count = 0
                self._window_dismiss_attempted = False
                self._window_dismiss_succeeded = False
                _LOGGER.info(
                    "Skip alarm: window OPENED for %s (alarm_id=%s, time_until=%s)",
                    self._who(),
                    current_alarm_id,
                    f"{time_until:.0f}s" if time_until is not None else "none",
                )
            else:
                _LOGGER.info(
                    "Skip alarm: window CLOSED for %s (alarm_id=%s, time_until=%s) — "
                    "max_empty=%d/%d, dismiss_attempted=%s, dismiss_succeeded=%s, "
                    "bed_occupied=%s, presence=%s",
                    self._who(),
                    current_alarm_id,
                    f"{time_until:.0f}s" if time_until is not None else "none",
                    self._window_max_empty_count,
                    _EMPTY_READINGS_REQUIRED,
                    self._window_dismiss_attempted,
                    self._window_dismiss_succeeded,
                    bed_occupied,
                    self._presence_reason(),
                )
            self._last_approaching = approaching

        if not approaching:
            self._empty_count = 0
            # Reset dismissed tracking when alarm cycle ends
            if self._dismissed_alarm_id and self._dismissed_alarm_id != current_alarm_id:
                self._dismissed_alarm_id = None
            return

        # Don't re-dismiss the same alarm
        if self._dismissed_alarm_id == current_alarm_id:
            return

        # A dismiss task is already running its retry loop — don't spawn
        # a second one. The running task will retry every 15s on 409 and
        # either succeed or give up after 90s. Without this guard, every
        # 30s coordinator poll would spawn another task, multiplying API
        # calls and fighting over the same alarm_id.
        if self._dismiss_in_flight:
            return

        if bed_occupied:
            self._empty_count = 0
            return

        self._empty_count += 1
        if self._empty_count > self._window_max_empty_count:
            self._window_max_empty_count = self._empty_count

        if self._empty_count >= _EMPTY_READINGS_REQUIRED:
            # Start dismissing a few seconds BEFORE nextTimestamp. The
            # Eight Sleep backend often accepts dismiss slightly before
            # its nominal ring time, which shortens how long the alarm
            # audibly rings. If the first attempt 409s, the retry loop
            # inside _dismiss_alarm will catch the "now ringing"
            # transition within 15s.
            if time_until is not None and time_until > _DISMISS_LEAD_SECONDS:
                return  # heartbeat already shows we are waiting
            _LOGGER.info(
                "Skip alarm: attempting dismiss for %s (alarm_id=%s, time_until=%s)",
                self._who(),
                current_alarm_id,
                f"{time_until:.0f}s" if time_until is not None else "none",
            )
            # Set the in-flight flag synchronously BEFORE scheduling the
            # task. async_create_task returns immediately and the task
            # starts on the next event-loop tick; another coordinator
            # update in between would otherwise see the flag still False
            # and spawn a duplicate dismiss task.
            self._dismiss_in_flight = True
            self._window_dismiss_attempted = True
            self.hass.async_create_task(self._dismiss_alarm(current_alarm_id))

    async def _dismiss_alarm(self, alarm_id: str) -> None:
        """Attempt dismiss with a tight retry loop.

        The Eight Sleep backend may 409 ("not ringing yet") for several
        seconds around nextTimestamp as the alarm transitions from
        pre-warm to ringing. Retry every 15s for up to 90s so we catch
        that transition quickly instead of waiting a full 30s coordinator
        poll between attempts. After 90s, bail out and let the coordinator
        respawn this task on the next poll — the outer dismiss window is
        5 minutes, so we still have plenty of opportunity.

        Guarded by self._dismiss_in_flight, which the caller sets
        synchronously before scheduling this task. We only need to clear
        it on exit via the finally block.
        """
        try:
            start = datetime.now(timezone.utc)
            attempt = 0
            while True:
                attempt += 1
                try:
                    success = await self._user_obj.alarm_dismiss(alarm_id)
                except Exception as err:  # noqa: BLE001 — log anything, caller is fire-and-forget
                    _LOGGER.warning(
                        "Skip alarm: dismiss raised unexpected exception for %s "
                        "(alarm_id=%s, attempt=%d): %s",
                        self._who(),
                        alarm_id,
                        attempt,
                        err,
                    )
                    return

                if success:
                    self._dismissed_alarm_id = alarm_id
                    self._window_dismiss_succeeded = True
                    self._attr_extra_state_attributes["last_auto_dismissed"] = (
                        datetime.now(timezone.utc).isoformat()
                    )
                    _LOGGER.info(
                        "Skip alarm: successfully dismissed alarm for %s "
                        "(alarm_id=%s, attempts=%d)",
                        self._who(),
                        alarm_id,
                        attempt,
                    )
                    await self.coordinator.async_request_refresh()
                    return

                elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                if elapsed >= _DISMISS_RETRY_MAX_SECONDS:
                    _LOGGER.debug(
                        "Skip alarm: dismiss still 409 after %ds for %s "
                        "(alarm_id=%s, attempts=%d) — releasing task, "
                        "coordinator will retry on next poll",
                        int(elapsed),
                        self._who(),
                        alarm_id,
                        attempt,
                    )
                    return

                _LOGGER.debug(
                    "Skip alarm: dismiss 409 for %s (alarm_id=%s, "
                    "attempt=%d, elapsed=%.0fs), retrying in %ds",
                    self._who(),
                    alarm_id,
                    attempt,
                    elapsed,
                    _DISMISS_RETRY_INTERVAL,
                )
                await asyncio.sleep(_DISMISS_RETRY_INTERVAL)
        finally:
            self._dismiss_in_flight = False
