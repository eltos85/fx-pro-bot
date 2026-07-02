"""Unit-тесты диспетчера ответов CTraderClient: корреляция по clientMsgId.

Регрессия бага 2026-07-02 (BUILDLOG): waiter матчился только по payloadType,
а один ордер/close порождает ≥2 ProtoOAExecutionEvent (ORDER_ACCEPTED +
ORDER_FILLED) — «хвостовое» событие съедалось waiter'ом СЛЕДУЮЩЕГО запроса.
Итог: чужой positionId/fill_price в open_position → ложный slippage-guard
(slip 16 057 pip = цена одного символа против цены другого), сделка помечена
failed при реально открытой позиции, повторный вход → дубль позиции.

Фикс: ProtoMessage.clientMsgId — «Request message id, assigned by the client
that will be returned in the response»
(https://help.ctrader.com/open-api/common-messages/#protomessage).
Server-push события БЕЗ clientMsgId waiter'ов не трогают.

Тесты без сети: _on_message вызывается напрямую с настоящими ProtoMessage.
"""

from __future__ import annotations

import threading

import pytest

from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoMessage
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAErrorRes,
    ProtoOAExecutionEvent,
    ProtoOAReconcileRes,
)

from fx_pro_bot.trading.client import CTraderClient


def _make_client() -> CTraderClient:
    c = CTraderClient(
        client_id="cid",
        client_secret="csec",
        access_token="atok",
        account_id=12345,
        host_type="demo",
        refresh_token="rtok",
    )
    # _on_message игнорирует сообщения от «чужого» client-объекта.
    c._client = object()
    return c


def _wrap(payload_msg, client_msg_id: str = "") -> ProtoMessage:
    pm = ProtoMessage()
    pm.payloadType = payload_msg.payloadType
    pm.payload = payload_msg.SerializeToString()
    if client_msg_id:
        pm.clientMsgId = client_msg_id
    return pm


def _reconcile_res() -> ProtoOAReconcileRes:
    res = ProtoOAReconcileRes()
    res.ctidTraderAccountId = 12345
    return res


def _execution_event() -> ProtoOAExecutionEvent:
    ev = ProtoOAExecutionEvent()
    ev.ctidTraderAccountId = 12345
    ev.executionType = 3  # ORDER_FILLED
    return ev


def _register(c: CTraderClient, msg_id: str) -> tuple[threading.Event, list]:
    event = threading.Event()
    result: list = [None, None]
    c._waiters[msg_id] = (event, result)
    return event, result


def test_response_routed_to_matching_client_msg_id() -> None:
    c = _make_client()
    ev_a, res_a = _register(c, "req-a")
    ev_b, res_b = _register(c, "req-b")

    c._on_message(c._client, _wrap(_reconcile_res(), "req-b"))

    assert ev_b.is_set() and res_b[0] is not None
    assert not ev_a.is_set() and res_a[0] is None, (
        "ответ не должен доставаться чужому waiter'у (регрессия payloadType-матчинга)"
    )
    assert "req-b" not in c._waiters and "req-a" in c._waiters


def test_push_event_without_client_msg_id_does_not_consume_waiter() -> None:
    """«Хвостовой» ORDER_FILLED / server-push без clientMsgId — не для waiter'ов.

    Именно этот сценарий давал чужой positionId в open_position.
    """
    c = _make_client()
    ev, res = _register(c, "req-open")

    c._on_message(c._client, _wrap(_execution_event()))  # без clientMsgId

    assert not ev.is_set() and res[0] is None
    assert "req-open" in c._waiters


def test_error_routed_only_to_matching_waiter() -> None:
    c = _make_client()
    ev_a, res_a = _register(c, "req-a")
    ev_b, res_b = _register(c, "req-b")

    err = ProtoOAErrorRes()
    err.errorCode = "MARKET_CLOSED"
    err.description = "Trading is not available"
    c._on_message(c._client, _wrap(err, "req-a"))

    assert ev_a.is_set() and isinstance(res_a[1], RuntimeError)
    assert "MARKET_CLOSED" in str(res_a[1])
    assert not ev_b.is_set() and res_b[1] is None


def test_orphan_error_without_client_msg_id_kills_no_waiter() -> None:
    """Ошибка без clientMsgId (server-push) не должна валить случайный запрос.

    Старый код брал «первый попавшийся» waiter из любого списка — ошибка
    guard-close уходила в waiter следующего open (07-02 08:02:05: открытие
    USDJPY «упало» с POSITION_NOT_FOUND чужого close).
    """
    c = _make_client()
    ev, res = _register(c, "req-x")

    err = ProtoOAErrorRes()
    err.errorCode = "POSITION_NOT_FOUND"
    c._on_message(c._client, _wrap(err))  # без clientMsgId

    assert not ev.is_set() and res[1] is None
    assert "req-x" in c._waiters


def test_send_and_wait_timeout_cleans_up_waiter() -> None:
    c = _make_client()
    c._connected.set()

    with pytest.raises(TimeoutError):
        c._send_and_wait(_reconcile_res(), ProtoOAReconcileRes().payloadType, timeout=0.05)

    assert not c._waiters, "после таймаута waiter должен быть удалён"
