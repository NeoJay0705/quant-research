"""Deterministic cash, holdings, and corporate-action ledger (no order execution)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from .models import (
    Bar,
    CorporateAction,
    ResearchInputError,
    UnsupportedDataError,
    iso_date,
    validate_action,
    validate_bar,
    validate_decimal,
)


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class Valuation:
    symbol: str
    quantity: Decimal
    price: Decimal
    price_date: str
    stale: bool


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    session: str
    cash: Decimal
    positions: tuple[Position, ...]
    receivables: Decimal
    pending_shares: tuple[Position, ...]
    nav: Decimal
    valuations: tuple[Valuation, ...]
    events: tuple[LedgerEvent, ...]
    stock_changed_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionUpdate:
    session: str
    events: tuple[LedgerEvent, ...]
    changed_symbols: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    action: CorporateAction
    effective_date: str
    eligible_quantity: Decimal
    cash_amount: Decimal
    shares_amount: Decimal
    cash_credit_date: str
    share_available_date: str
    cash_date_assumed: bool
    share_date_assumed: bool
    policy: str
    source_refs: tuple[str, ...]
    cash_before: Decimal
    cash_after: Decimal
    shares_before: Decimal
    shares_after: Decimal
    kind: str = "action"
    record_date: str | None = None


@dataclass(frozen=True, slots=True)
class CashReceivable:
    symbol: str
    amount: Decimal
    credit_date: str
    event: LedgerEvent


class Ledger:
    """Pure holdings ledger. Call ``process_session`` once per increasing session date."""

    def __init__(
        self,
        initial_cash: Decimal,
        initial_holdings: dict[str, Decimal] | None = None,
        events: tuple[CorporateAction, ...] | list[CorporateAction] = (),
    ) -> None:
        validate_decimal(initial_cash, "initial_cash", minimum=Decimal(0))
        if initial_cash is None:
            raise ResearchInputError("initial_cash is required")
        self._cash = initial_cash
        self._positions: dict[str, Decimal] = {}
        for symbol, qty in (initial_holdings or {}).items():
            if not isinstance(symbol, str) or not symbol:
                raise ResearchInputError("holding symbols must be non-empty strings")
            validate_decimal(qty, "initial holding", minimum=Decimal(0))
            if qty is None:
                raise ResearchInputError("initial holding quantity is required")
            if qty:
                self._positions[symbol] = qty
        for action in events:
            validate_action(action)
        self._actions = tuple(sorted(events, key=lambda a: (a.effective_date, a.symbol)))
        self._cursor = 0
        self._receivables: list[CashReceivable] = []
        self._pending: dict[str, Decimal] = {}
        self._pending_event: dict[str, CorporateAction] = {}
        self._last_prices: dict[str, tuple[Decimal, str]] = {}
        self._last_session: str | None = None
        self._open_session: str | None = None
        self._open_events: tuple[LedgerEvent, ...] = ()
        self._open_changed: tuple[str, ...] = ()
        self._freshness_required: set[str] = set()
        self._all_events: list[LedgerEvent] = []
        self._snapshots: list[AccountSnapshot] = []

    @property
    def cash(self) -> Decimal:
        return self._cash

    @property
    def receivables(self) -> tuple[CashReceivable, ...]:
        return tuple(self._receivables)

    @property
    def positions(self) -> tuple[Position, ...]:
        return tuple(Position(s, q) for s, q in sorted(self._positions.items()))

    @property
    def events(self) -> tuple[LedgerEvent, ...]:
        return tuple(self._all_events)

    @property
    def snapshots(self) -> tuple[AccountSnapshot, ...]:
        return tuple(self._snapshots)

    def apply_buy(
        self, symbol: str, quantity: Decimal, price: Decimal, transaction_cost: Decimal = Decimal(0)
    ) -> None:
        """Book a completed buy; transaction_cost is aggregate fee and tax."""
        self._trade(symbol, quantity, price, transaction_cost, buy=True)

    def apply_sell(
        self, symbol: str, quantity: Decimal, price: Decimal, transaction_cost: Decimal = Decimal(0)
    ) -> None:
        """Book a completed sell; transaction_cost is aggregate fee and tax."""
        self._trade(symbol, quantity, price, transaction_cost, buy=False)

    def _trade(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        transaction_cost: Decimal,
        *,
        buy: bool,
    ) -> None:
        if not isinstance(symbol, str) or not symbol:
            raise ResearchInputError("symbol must be non-empty")
        validate_decimal(quantity, "quantity", minimum=Decimal(0))
        validate_decimal(price, "price", minimum=Decimal(0))
        validate_decimal(transaction_cost, "transaction_cost", minimum=Decimal(0))
        if quantity is None or price is None or quantity == 0 or price == 0:
            raise ResearchInputError("quantity and price must be positive")
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            value = quantity * price
            debit = value + transaction_cost
            current = self._positions.get(symbol, Decimal(0))
            if buy:
                if debit > self._cash:
                    raise ResearchInputError("insufficient cash")
                self._cash -= debit
                self._positions[symbol] = current + quantity
            else:
                if quantity > current:
                    raise ResearchInputError("insufficient holdings")
                proceeds = value - transaction_cost
                if self._cash + proceeds < 0:
                    raise ResearchInputError("insufficient cash to cover sale costs")
                self._cash += proceeds
                remaining = current - quantity
                if remaining:
                    self._positions[symbol] = remaining
                else:
                    self._positions.pop(symbol, None)

    def begin_session(self, session: str, pending_symbols: tuple[str, ...] = ()) -> SessionUpdate:
        """Advance effective actions and cash/share releases in calendar order.

        LedgerEvent.kind distinguishes entitlement ``action`` records from ``cash_release``
        and ``share_release`` audit records; record_date is the date the transition booked.
        Receivables retain their actual entitlement event, never a synthetic placeholder.
        """
        iso_date(session, "session")
        if self._open_session is not None:
            raise ResearchInputError("a ledger session is already open")
        if self._last_session is not None and session <= self._last_session:
            raise ResearchInputError("sessions must be strictly increasing")
        ledgers: list[LedgerEvent] = []
        changed: set[str] = set()
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            upcoming = [a for a in self._actions[self._cursor :] if a.effective_date <= session]
            timeline = sorted(
                {a.effective_date for a in upcoming}
                | {r.credit_date for r in self._receivables if r.credit_date <= session}
                | {
                    (a.share_available_date or a.effective_date)
                    for a in self._pending_event.values()
                    if (a.share_available_date or a.effective_date) <= session
                }
                | {
                    a.cash_credit_date
                    for a in upcoming
                    if a.cash_credit_date and a.cash_credit_date <= session
                }
                | {
                    a.share_available_date
                    for a in upcoming
                    if a.share_available_date and a.share_available_date <= session
                }
            )
            for day in timeline:
                for rec in tuple(self._receivables):
                    if rec.credit_date == day:
                        before = self._cash
                        self._cash += rec.amount
                        original = rec.event
                        released = LedgerEvent(
                            original.action,
                            original.effective_date,
                            original.eligible_quantity,
                            original.cash_amount,
                            original.shares_amount,
                            original.cash_credit_date,
                            original.share_available_date,
                            original.cash_date_assumed,
                            original.share_date_assumed,
                            original.policy,
                            original.source_refs,
                            before,
                            self._cash,
                            self._positions.get(rec.symbol, Decimal(0)),
                            self._positions.get(rec.symbol, Decimal(0)),
                            "cash_release",
                            day,
                        )
                        ledgers.append(released)
                        self._all_events.append(released)
                        self._receivables.remove(rec)
                for symbol, qty in tuple(self._pending.items()):
                    action = self._pending_event[symbol]
                    if (action.share_available_date or action.effective_date) == day:
                        before = self._positions.get(symbol, Decimal(0))
                        self._positions[symbol] = before + qty
                        source_event = next(
                            (e for e in reversed(self._all_events) if e.action is action), None
                        )
                        if source_event is not None:
                            released = LedgerEvent(
                                source_event.action,
                                source_event.effective_date,
                                source_event.eligible_quantity,
                                source_event.cash_amount,
                                source_event.shares_amount,
                                source_event.cash_credit_date,
                                source_event.share_available_date,
                                source_event.cash_date_assumed,
                                source_event.share_date_assumed,
                                source_event.policy,
                                source_event.source_refs,
                                self._cash,
                                self._cash,
                                before,
                                before + qty,
                                "share_release",
                                day,
                            )
                            ledgers.append(released)
                            self._all_events.append(released)
                        self._pending.pop(symbol)
                        self._pending_event.pop(symbol)
                todays: list[CorporateAction] = []
                while (
                    self._cursor < len(self._actions)
                    and self._actions[self._cursor].effective_date == day
                ):
                    a = self._actions[self._cursor]
                    self._cursor += 1
                    if self._last_session is not None and a.effective_date <= self._last_session:
                        raise ResearchInputError(
                            "event effective date was skipped by session processing"
                        )
                    todays.append(a)
                by_symbol: dict[str, list[CorporateAction]] = {}
                for a in todays:
                    by_symbol.setdefault(a.symbol, []).append(a)
                for symbol, actions in by_symbol.items():
                    quantity = self._positions.get(symbol, Decimal(0))
                    has_intent = symbol in pending_symbols
                    if self._pending.get(symbol):
                        raise UnsupportedDataError(
                            f"{symbol}: pending shares cross another stock change"
                        )
                    if not quantity and not has_intent:
                        continue
                    if len(actions) > 1 and quantity:
                        raise UnsupportedDataError(
                            f"{symbol}: ambiguous same-day corporate actions"
                        )
                    action = actions[0]
                    affected = quantity > 0 or symbol in pending_symbols
                    self._unsupported_markers(action, affected)
                    if (
                        quantity
                        and action.event_type == "dividend"
                        and action.bonus_share_ratio is None
                    ):
                        raise UnsupportedDataError(
                            f"{symbol}: dividend bonus-share ratio must be explicitly known"
                        )
                    if not quantity:
                        if action.event_type in {"capital_reduction", "par_value"}:
                            try:
                                multiplier = self._multiplier(action)
                            except UnsupportedDataError:
                                # Existing old-unit intent is unsafe even when the exact ratio
                                # cannot be derived; there are no shares to transform.
                                multiplier = None
                            if multiplier is None or multiplier != 1:
                                changed.add(symbol)
                        elif (
                            action.event_type == "dividend"
                            and (action.bonus_share_ratio or Decimal(0)) > 0
                        ):
                            changed.add(symbol)
                        continue
                    if action.event_type not in {"dividend", "capital_reduction", "par_value"}:
                        raise UnsupportedDataError(
                            f"{symbol}: unsupported corporate-action type {action.event_type!r}"
                        )
                    multiplier = (
                        self._multiplier(action)
                        if action.event_type in {"capital_reduction", "par_value"}
                        else None
                    )
                    bonus = action.bonus_share_ratio or Decimal(0)
                    real_units = (multiplier is not None and multiplier != 1) or bonus > 0
                    if real_units:
                        changed.add(symbol)
                    if real_units or action.dividend or action.cash_return_per_share:
                        self._freshness_required.add(symbol)
                    cash_before, shares_before = self._cash, quantity
                    cash_amt = quantity * (action.dividend or Decimal(0)) + quantity * (
                        action.cash_return_per_share or Decimal(0)
                    )
                    shares_amt = quantity * bonus
                    cash_date, share_date = (
                        action.cash_credit_date or day,
                        action.share_available_date or day,
                    )
                    cash_assumed, share_assumed = (
                        action.cash_credit_date is None,
                        action.share_available_date is None,
                    )
                    policy = (
                        "effective_date_proxy" if cash_assumed or share_assumed else "source_dates"
                    )
                    if cash_amt and cash_date == day:
                        self._cash += cash_amt
                    newqty = quantity * multiplier if multiplier is not None else quantity
                    if shares_amt:
                        if share_date == day:
                            newqty += shares_amt
                        else:
                            self._pending[symbol] = (
                                self._pending.get(symbol, Decimal(0)) + shares_amt
                            )
                            self._pending_event[symbol] = action
                    self._positions[symbol] = newqty
                    event_record = LedgerEvent(
                        action,
                        day,
                        quantity,
                        cash_amt,
                        shares_amt,
                        cash_date,
                        share_date,
                        cash_assumed,
                        share_assumed,
                        policy,
                        action.source_refs,
                        cash_before,
                        self._cash,
                        shares_before,
                        newqty,
                        "action",
                        day,
                    )
                    self._all_events.append(event_record)
                    ledgers.append(event_record)
                    if cash_amt and cash_date > day:
                        self._receivables.append(
                            CashReceivable(symbol, cash_amt, cash_date, event_record)
                        )
            self._open_session = session
            self._open_events = tuple(ledgers)
            self._open_changed = tuple(sorted(changed))
            return SessionUpdate(session, self._open_events, self._open_changed)

    def close_session(self, session: str, bars: tuple[Bar, ...] | list[Bar]) -> AccountSnapshot:
        """Value an open session after all transactions have been booked."""
        if self._open_session != session:
            raise ResearchInputError("close_session must match the open session")
        bar_map: dict[str, Bar] = {}
        for bar in bars:
            validate_bar(bar)
            if bar.date != session or bar.symbol in bar_map:
                raise ResearchInputError("bars must have unique symbols and match session")
            bar_map[bar.symbol] = bar
            if bar.close is not None and bar.close > 0:
                self._last_prices[bar.symbol] = (bar.close, session)
        missing = self._freshness_required - {
            s for s, b in bar_map.items() if b.close is not None and b.close > 0
        }
        if missing:
            symbol = sorted(missing)[0]
            raise UnsupportedDataError(
                f"{symbol}: current close required after entitlement/share-unit change on {session}"
            )
        with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
            valuation: list[Valuation] = []
            nav = self._cash + sum((r.amount for r in self._receivables), Decimal(0))
            for symbol in sorted(set(self._positions) | set(self._pending)):
                q = self._positions.get(symbol, Decimal(0)) + self._pending.get(symbol, Decimal(0))
                current = bar_map.get(symbol)
                cached = self._last_prices.get(symbol)
                if current and current.close is not None and current.close > 0:
                    price, pdate, stale = current.close, session, False
                elif cached:
                    price, pdate = cached
                    stale = True
                else:
                    raise UnsupportedDataError(
                        f"{symbol}: no valid close available for valuation on {session}"
                    )
                valuation.append(Valuation(symbol, q, price, pdate, stale))
                nav += q * price
            snapshot = AccountSnapshot(
                session,
                self._cash,
                self.positions,
                sum((r.amount for r in self._receivables), Decimal(0)),
                tuple(Position(s, q) for s, q in sorted(self._pending.items())),
                nav,
                tuple(valuation),
                self._open_events,
                self._open_changed,
            )
            self._last_session = session
            self._snapshots.append(snapshot)
            self._open_session = None
            self._freshness_required.clear()
            return snapshot

    def process_session(
        self,
        session: str,
        bars: tuple[Bar, ...] | list[Bar],
        *,
        pending_symbols: tuple[str, ...] = (),
    ) -> AccountSnapshot:
        """Compatibility convenience: begin dated events, then close and value."""
        self.begin_session(session, pending_symbols)
        return self.close_session(session, bars)

    @staticmethod
    def _multiplier(action: CorporateAction) -> Decimal | None:
        direct = action.share_multiplier
        par_ratio = None
        if action.par_before is not None and action.par_after is not None:
            with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
                par_ratio = action.par_before / action.par_after
        if direct is not None and par_ratio is not None and direct != par_ratio:
            raise UnsupportedDataError(
                f"{action.symbol}: share multiplier conflicts with par ratio"
            )
        if action.event_type == "capital_reduction" and direct is None:
            raise UnsupportedDataError(
                f"{action.symbol}: capital reduction requires share_multiplier"
            )
        value = (
            direct
            if direct is not None
            else (par_ratio if action.event_type == "par_value" else None)
        )
        if (
            value is None
            and action.event_type in {"capital_reduction", "par_value"}
            and (action.before_price is not None or action.reference_price is not None)
        ):
            raise UnsupportedDataError(f"{action.symbol}: price-only share ratio is unsupported")
        return value

    @staticmethod
    def _unsupported_markers(action: CorporateAction, affected: bool) -> None:
        if not affected:
            return
        explicit_flags = dict(action.payload)
        exact_markers = {
            "cash_capital_increase_present",
            "employee_bonus_shares_present",
        }
        if any(flag in action.quality_flags or flag in action.reasons for flag in exact_markers):
            raise UnsupportedDataError(f"{action.symbol}: unsupported paid/employee shares")
        if any(
            str(explicit_flags.get(flag, "")).strip().lower() == "true" for flag in exact_markers
        ):
            raise UnsupportedDataError(f"{action.symbol}: unsupported paid/employee shares")
        text = " ".join((action.event_type, *action.quality_flags, *action.reasons)).lower()
        if any(
            word in text
            for word in (
                "subscription",
                "employee",
                "paid_allot",
                "conflict",
                "unsupported",
                "ambiguous",
            )
        ):
            raise UnsupportedDataError(
                f"{action.symbol}: unsupported/conflicting corporate-action condition: {text}"
            )
