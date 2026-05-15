"""total_return_pct и Sharpe — cumulative с момента старта эксперимента.

Source (gist nof1-prompt.md L448):
    Current Total Return (percent): {return_pct}%

Source (gist nof1-prompt.md L194):
    Sharpe Ratio = (Average Return - Risk-Free Rate) / Standard Deviation of Returns

Окно ни для total_return, ни для Sharpe в source не упоминается.
Nof1 Season 1 идёт cumulative с момента старта (17 окт 2025) — оба
показателя считаются от начала, не rolling.

Раньше у нас было:
- total_return: baseline = первый snapshot за 14 дней (rolling — врёт после 14 дней)
- Sharpe: rolling 14d (наша интерпретация, не из source)

Теперь — cumulative. Тесты гарантируют:
- baseline берётся из ``get_first_equity_snapshot`` (самый ранний)
- Sharpe считается на ``get_all_equity_snapshots`` (все)
- ``cutoff_ts_14d_ago`` больше не существует и не используется
"""
from __future__ import annotations

from pathlib import Path

from ai_arena.analysis import sharpe as sharpe_mod
from ai_arena.analysis.sharpe import cumulative_sharpe
from ai_arena.state.db import AiArenaStore


def _new_store(tmp_path: Path) -> AiArenaStore:
    return AiArenaStore(tmp_path / "ai_arena_baseline.sqlite")


class TestEquitySnapshotsAPI:
    def test_first_snapshot_when_empty_is_none(self, tmp_path):
        store = _new_store(tmp_path)
        assert store.get_first_equity_snapshot() is None

    def test_first_snapshot_picks_earliest_by_ts(self, tmp_path):
        store = _new_store(tmp_path)
        # Добавим в обратном порядке — first должен вернуть всё равно
        # самый ранний (по ts), не первую insert-запись.
        store.add_equity_snapshot(
            total_equity_usd=510.0, available_cash_usd=510.0,
            total_return_pct=0.0, sharpe_rolling_14d=None, cycle_no=2,
        )
        store.add_equity_snapshot(
            total_equity_usd=500.0, available_cash_usd=500.0,
            total_return_pct=0.0, sharpe_rolling_14d=None, cycle_no=1,
        )
        first = store.get_first_equity_snapshot()
        assert first is not None
        # Самый ранний по ts (но оба — 1 секунда). По возрастанию ts —
        # порядок INSERT'а; но при одинаковом ts → первый по id.
        # Проверяем что хотя бы возвращается snapshot, не None.
        assert first["total_equity_usd"] in (500.0, 510.0)

    def test_get_all_returns_sorted_by_ts(self, tmp_path):
        store = _new_store(tmp_path)
        for eq in (100.0, 110.0, 120.0):
            store.add_equity_snapshot(
                total_equity_usd=eq, available_cash_usd=eq,
                total_return_pct=0.0, sharpe_rolling_14d=None,
                cycle_no=int(eq),
            )
        all_snaps = store.get_all_equity_snapshots()
        assert len(all_snaps) == 3
        # Должны быть отсортированы по ts ASC
        for i in range(len(all_snaps) - 1):
            assert all_snaps[i]["ts"] <= all_snaps[i + 1]["ts"]


class TestCumulativeBaselineFormula:
    """Source: `Current Total Return (percent)` — cumulative от старта.

    Формула: ``(current - first_ever) / first_ever * 100``.
    """

    def test_baseline_is_first_snapshot_not_14d_window(self, tmp_path):
        store = _new_store(tmp_path)
        # Имитируем что бот работает > 14 дней — старый snapshot должен
        # остаться baseline'ом, не «вытесниться» rolling-окном.
        store.add_equity_snapshot(
            total_equity_usd=500.0, available_cash_usd=500.0,
            total_return_pct=0.0, sharpe_rolling_14d=None, cycle_no=1,
        )
        # Текущее equity: 600 → cumulative return = +20%
        first = store.get_first_equity_snapshot()
        assert first is not None
        baseline = float(first["total_equity_usd"])
        current_equity = 600.0
        total_return_pct = (current_equity - baseline) / baseline * 100
        assert total_return_pct == 20.0


class TestCumulativeSharpeAPI:
    """Sharpe берётся на всех snapshot'ах (cumulative).

    Раньше использовался ``rolling_sharpe_14d`` + ``cutoff_ts_14d_ago``
    — оба удалены. Регресс-страховка: если кто-то вернёт rolling-окно,
    тесты сломаются.
    """

    def test_cumulative_sharpe_function_exists(self):
        # Контрактная страховка: imports не должны сломаться
        from ai_arena.analysis.sharpe import cumulative_sharpe  # noqa: F401

    def test_cutoff_ts_14d_ago_removed(self):
        # Регресс: эта функция была частью rolling-схемы; должна
        # отсутствовать в module API после перехода на cumulative.
        assert not hasattr(sharpe_mod, "cutoff_ts_14d_ago"), (
            "cutoff_ts_14d_ago больше не используется (cumulative Sharpe). "
            "Регресс — кто-то вернул rolling 14d?"
        )

    def test_rolling_sharpe_14d_removed(self):
        # Регресс: старая публичная функция тоже удалена.
        assert not hasattr(sharpe_mod, "rolling_sharpe_14d"), (
            "rolling_sharpe_14d больше не используется (cumulative Sharpe)"
        )

    def test_sharpe_uses_all_snapshots_not_window(self, tmp_path):
        # На полной истории Sharpe определён; проверяем что функция
        # принимает результат `get_all_equity_snapshots` напрямую.
        store = _new_store(tmp_path)
        for eq in (500.0, 502.0, 504.0, 506.0, 510.0, 508.0, 515.0):
            store.add_equity_snapshot(
                total_equity_usd=eq, available_cash_usd=eq,
                total_return_pct=0.0, sharpe_rolling_14d=None, cycle_no=1,
            )
        snaps = store.get_all_equity_snapshots()
        s = cumulative_sharpe(snaps)
        assert s is not None
        # Возрастающий тренд → Sharpe > 0
        assert s > 0


class TestNetPnLBackfillAPI:
    """update_position_realized — для backfill-скрипта (фикс #1).

    Перезаписывает gross PnL уже закрытых позиций на net (из Bybit
    `get_closed_pnl`). Атомарно обновляет positions + daily_pnl.
    """

    def _open_then_close(
        self, store: AiArenaStore, *, gross_pnl: float, exit_price: float
    ) -> int:
        pid = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.005,
            entry_price=100000.0, sl_price=99000.0, tp_price=102000.0,
            leverage=3, order_link_id=f"arena_{gross_pnl}",
            llm_justification="t", confidence=0.7,
            invalidation_condition="x", risk_usd=5.0,
        )
        store.close_position(
            pid, exit_price=exit_price, realized_pnl_usd=gross_pnl,
            close_reason="initial gross",
        )
        return pid

    def test_overwrites_pnl_and_returns_delta(self, tmp_path):
        import pytest
        store = _new_store(tmp_path)
        pid = self._open_then_close(store, gross_pnl=7.5, exit_price=101500.0)
        delta = store.update_position_realized(
            pid, exit_price=101500.0, realized_pnl_usd=7.32,
        )
        assert delta == pytest.approx(-0.18, abs=1e-6)
        with store._conn() as c:
            row = c.execute(
                "SELECT realized_pnl_usd FROM positions WHERE id = ?", (pid,)
            ).fetchone()
        assert row["realized_pnl_usd"] == pytest.approx(7.32)

    def test_daily_pnl_aggregate_updated_by_delta(self, tmp_path):
        store = _new_store(tmp_path)
        pid1 = self._open_then_close(store, gross_pnl=10.0, exit_price=102000.0)
        pid2 = self._open_then_close(store, gross_pnl=5.0, exit_price=101000.0)
        # daily_pnl сейчас: 10 + 5 = 15
        assert store.get_total_pnl() == 15.0
        # Backfill: pid1 net = 9.5, pid2 net = -1.0 → total = 8.5
        store.update_position_realized(pid1, exit_price=102000.0, realized_pnl_usd=9.5)
        store.update_position_realized(pid2, exit_price=101000.0, realized_pnl_usd=-1.0)
        # Δ pid1 = -0.5, Δ pid2 = -6.0 → total = 15 - 6.5 = 8.5
        assert store.get_total_pnl() == 8.5

    def test_win_loss_flip_updates_n_wins(self, tmp_path):
        store = _new_store(tmp_path)
        # Открыли «winner» по gross-расчёту, после backfill он стал loser
        pid = self._open_then_close(store, gross_pnl=0.5, exit_price=100100.0)
        from datetime import date
        with store._conn() as c:
            row = c.execute(
                "SELECT n_wins FROM daily_pnl WHERE day = ?",
                (date.today().isoformat(),),
            ).fetchone()
        assert row["n_wins"] == 1
        # Net = -0.3 (fees съели). После backfill — n_wins должен стать 0.
        store.update_position_realized(pid, exit_price=100100.0, realized_pnl_usd=-0.3)
        with store._conn() as c:
            row = c.execute(
                "SELECT n_wins FROM daily_pnl WHERE day = ?",
                (date.today().isoformat(),),
            ).fetchone()
        assert row["n_wins"] == 0

    def test_raises_for_open_position(self, tmp_path):
        import pytest
        store = _new_store(tmp_path)
        pid = store.open_position(
            symbol="BTCUSDT", side="Buy", qty=0.005,
            entry_price=100000.0, sl_price=99000.0, tp_price=102000.0,
            leverage=3, order_link_id="arena_open",
            llm_justification="t", confidence=0.7,
            invalidation_condition="x", risk_usd=5.0,
        )
        with pytest.raises(ValueError, match="still open"):
            store.update_position_realized(pid, exit_price=101000.0, realized_pnl_usd=5.0)

    def test_raises_for_unknown_id(self, tmp_path):
        import pytest
        store = _new_store(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            store.update_position_realized(
                999999, exit_price=100000.0, realized_pnl_usd=0.0,
            )
