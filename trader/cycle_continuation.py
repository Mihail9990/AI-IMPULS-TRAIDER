"""Exclusive state-machine owner for recovery-cycle continuation attempts.

The normal Bot controller hands ownership to this object after a flat, confirmed double-SL.
Broker I/O and strategy arithmetic remain shared with Bot/Strategy, but only this dispatcher may
advance continuation pause, filter, pair formation, and subsequent cycle events.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .app import Bot


LOG = logging.getLogger(__name__)


class CycleContinuation:
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    @property
    def state(self):
        return self.bot.state

    def take_ownership(self, stage: str) -> None:
        self.state.continuation_managed = True
        self.state.continuation_stage = stage
        self.state.save(self.bot.cfg.state_file)

    def release(self) -> None:
        self.state.continuation_managed = False
        self.state.continuation_stage = ""

    def tick(self) -> None:
        """Advance exactly one continuation state; normal Bot.tick must not run in parallel."""
        stage = self.state.continuation_stage
        if stage == "RECONCILING":
            self.bot._tick_double_sl_reconciling()
        elif stage == "PAUSE":
            self.bot._tick_continuation_pause()
        elif stage == "FILTER":
            if not self.state.paused:
                self.bot._tick_filter(self.start_pair)
        elif stage == "FORMING_PAIR":
            # A persisted INITIAL submission is always resolved before any further POST. This is
            # the critical restart path for attempt 255.
            if any(leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)):
                self.bot._resume_pending_market()
                return
            if self.state.long and self.state.short and self.state.long.open and self.state.short.open:
                self.state.continuation_stage = "ACTIVE"
                self.state.save(self.bot.cfg.state_file)
                self.bot._tick_cycle()
        elif stage == "MANUAL_PAIR_PAUSE":
            return
        elif stage == "ACTIVE":
            self.bot._tick_cycle()
        else:
            LOG.error("Unknown continuation stage %r; blocking entries", stage)
            self.state.paused = True
            self.state.save(self.bot.cfg.state_file)

    def start_pair(self, filter_reason: str) -> None:
        """Own formation of a repeated pair without invoking initial-cycle state resets."""
        self.state.continuation_stage = "FORMING_PAIR"
        self.state.save(self.bot.cfg.state_file)
        self.bot._start_pair_common(filter_reason, continuation=True)

    def start_pause(self) -> None:
        self.state.continuation_stage = "PAUSE"
        self.state.continuation_pause_until = time.time() + 300
        self.state.save(self.bot.cfg.state_file)

    def filter_ready(self) -> None:
        self.state.continuation_stage = "FILTER"
        self.state.save(self.bot.cfg.state_file)

    def block_unknown(self) -> None:
        self.take_ownership("RECONCILING")

    def finish_single_leg(self, leg, source: str, fill: Decimal) -> None:
        """Persist a failed repeated-pair leg without resetting the parent recovery cycle."""
        event_id = f"continuation-single:{leg.deal_id}:{source}:{fill}"
        if event_id not in self.state.processed_events:
            loss = max(Decimal("0"), leg.current_entry - fill) if leg.direction == "BUY" else max(
                Decimal("0"), fill - leg.current_entry
            )
            self.state.realized_losses += loss
            self.state.processed_events.append(event_id)
            self.state.remember_deal(leg, self.state.scenario)
            self.state.remember_close(leg.deal_id, source, fill)
        result = -(self.state.realized_losses - self.state.cycle_attempt_start_losses) * self.bot.cfg.size
        self.state.remember_attempt(
            "CONTINUATION_PAIR_NOT_FORMED", result, scenario=self.state.scenario,
            direction=leg.direction, deal_id=leg.deal_id, entry=leg.current_entry,
            close=fill, close_source=source, completed_cycle=None,
        )
        self.state.long = self.state.short = None
        self.state.active_attempt_id = 0
        self.state.paused = True
        self.state.armed = False
        self.state.phase = "CONTINUATION_MANUAL_PAIR_PAUSE"
        self.state.continuation_stage = "MANUAL_PAIR_PAUSE"
        self.state.save(self.bot.cfg.state_file)
        self.bot.telegram.send(
            f"⏸ Продолжение цикла №{self.state.cycle_id}: пара не сформирована.\n"
            f"{leg.direction} {leg.deal_id}: вход {leg.current_entry}, {source} {fill}.\n"
            f"Результат попытки: {result}; накопленные потери цикла: "
            f"{self.state.realized_losses * self.bot.cfg.size}.\n"
            "Автоматический повтор отключён. После проверки брокера продолжение — только /start."
        )
