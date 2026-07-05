"""MEXC spot v3 protobuf-сообщения (runtime-descriptor, без protoc).

Схема — официальная: https://www.mexc.com/api-docs/spot-v3/websocket-market-streams/
protocol-buffers-integration (репозиторий https://github.com/mexcdevelop/websocket-proto).
.proto-файлы не парсятся runtime-библиотекой protobuf — поэтому FileDescriptorProto
строится программно (protobuf 3.20.1, без grpc_tools/protoc — api-docs.mdc: источник
правды = официальная дока/схема, цитируемая здесь).

Нужные для M1 сообщения:
- PushDataV3ApiWrapper  — обёртка WS-пуша (oneof body: publicAggreDepths=313,
  publicAggreDeals=314) + channel/symbol/symbolId/createTime/sendTime.
- PublicAggreDepthsV3Api — asks[], bids[], eventType, fromVersion, toVersion
  (version — строки, парсим в int).
- PublicAggreDepthV3ApiItem — price, quantity (строки).
- PublicAggreDealsV3Api   — deals[], eventType.
- PublicDealsV3ApiItem    — price, quantity (строки), tradeType (int32, 1=Buy/2=Sell),
  time (int64, ms).

Все остальные oneof-варианты обёртки (private*, kline, miniTicker, bookTicker, …)
нам не нужны — в descriptor они не добавляются (parser просто не заполнит body).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from google.protobuf import descriptor_pb2 as _dpb
from google.protobuf import message_factory as _mf

_PACKAGE = "mexc.spot.v3"
_FILE = f"{_PACKAGE}.proto"

# oneof-индексы обёртки (поле body). Поля 313/314.
_ONEOF_BODY = "body"
_BODY_PUBLIC_AGGRE_DEPTHS = 313   # PublicAggreDepthsV3Api
_BODY_PUBLIC_AGGRE_DEALS = 314    # PublicAggreDealsV3Api


def _file_descriptor() -> _dpb.FileDescriptorProto:
    f = _dpb.FileDescriptorProto()
    f.name = _FILE
    f.package = _PACKAGE
    f.syntax = "proto3"

    # PublicAggreDepthV3ApiItem { string price=1; string quantity=2; }
    m = f.message_type.add()
    m.name = "PublicAggreDepthV3ApiItem"
    fld = m.field.add(); fld.name = "price"; fld.number = 1
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld = m.field.add(); fld.name = "quantity"; fld.number = 2
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL

    # PublicAggreDepthsV3Api { repeated asks=1; repeated bids=2; string eventType=3;
    #   string fromVersion=4; string toVersion=5; }
    m = f.message_type.add()
    m.name = "PublicAggreDepthsV3Api"
    fld = m.field.add(); fld.name = "asks"; fld.number = 1
    fld.type = _dpb.FieldDescriptorProto.TYPE_MESSAGE
    fld.label = _dpb.FieldDescriptorProto.LABEL_REPEATED
    fld.type_name = f".{_PACKAGE}.PublicAggreDepthV3ApiItem"
    fld = m.field.add(); fld.name = "bids"; fld.number = 2
    fld.type = _dpb.FieldDescriptorProto.TYPE_MESSAGE
    fld.label = _dpb.FieldDescriptorProto.LABEL_REPEATED
    fld.type_name = f".{_PACKAGE}.PublicAggreDepthV3ApiItem"
    fld = m.field.add(); fld.name = "event_type"; fld.number = 3
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "eventType"
    fld = m.field.add(); fld.name = "from_version"; fld.number = 4
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "fromVersion"
    fld = m.field.add(); fld.name = "to_version"; fld.number = 5
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "toVersion"

    # PublicDealsV3ApiItem { string price=1; string quantity=2; int32 tradeType=3; int64 time=4; }
    m = f.message_type.add()
    m.name = "PublicDealsV3ApiItem"
    fld = m.field.add(); fld.name = "price"; fld.number = 1
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld = m.field.add(); fld.name = "quantity"; fld.number = 2
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld = m.field.add(); fld.name = "trade_type"; fld.number = 3
    fld.type = _dpb.FieldDescriptorProto.TYPE_INT32; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "tradeType"
    fld = m.field.add(); fld.name = "time"; fld.number = 4
    fld.type = _dpb.FieldDescriptorProto.TYPE_INT64; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL

    # PublicAggreDealsV3Api { repeated deals=1; string eventType=2; }
    m = f.message_type.add()
    m.name = "PublicAggreDealsV3Api"
    fld = m.field.add(); fld.name = "deals"; fld.number = 1
    fld.type = _dpb.FieldDescriptorProto.TYPE_MESSAGE
    fld.label = _dpb.FieldDescriptorProto.LABEL_REPEATED
    fld.type_name = f".{_PACKAGE}.PublicDealsV3ApiItem"
    fld = m.field.add(); fld.name = "event_type"; fld.number = 2
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "eventType"

    # PushDataV3ApiWrapper { string channel=1; oneof body { publicAggreDepths=313;
    #   publicAggreDeals=314; } string symbol=3; string symbolId=4; int64 createTime=5;
    #   int64 sendTime=6; }
    m = f.message_type.add()
    m.name = "PushDataV3ApiWrapper"
    fld = m.field.add(); fld.name = "channel"; fld.number = 1
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    oneof = m.oneof_decl.add(); oneof.name = _ONEOF_BODY
    fld = m.field.add(); fld.name = "public_aggre_depths"; fld.number = _BODY_PUBLIC_AGGRE_DEPTHS
    fld.type = _dpb.FieldDescriptorProto.TYPE_MESSAGE
    fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.type_name = f".{_PACKAGE}.PublicAggreDepthsV3Api"
    fld.json_name = "publicAggreDepths"; fld.oneof_index = 0
    fld = m.field.add(); fld.name = "public_aggre_deals"; fld.number = _BODY_PUBLIC_AGGRE_DEALS
    fld.type = _dpb.FieldDescriptorProto.TYPE_MESSAGE
    fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.type_name = f".{_PACKAGE}.PublicAggreDealsV3Api"
    fld.json_name = "publicAggreDeals"; fld.oneof_index = 0
    fld = m.field.add(); fld.name = "symbol"; fld.number = 3
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld = m.field.add(); fld.name = "symbol_id"; fld.number = 4
    fld.type = _dpb.FieldDescriptorProto.TYPE_STRING; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "symbolId"
    fld = m.field.add(); fld.name = "create_time"; fld.number = 5
    fld.type = _dpb.FieldDescriptorProto.TYPE_INT64; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "createTime"
    fld = m.field.add(); fld.name = "send_time"; fld.number = 6
    fld.type = _dpb.FieldDescriptorProto.TYPE_INT64; fld.label = _dpb.FieldDescriptorProto.LABEL_OPTIONAL
    fld.json_name = "sendTime"

    return f


@lru_cache(maxsize=1)
def _classes() -> dict[str, Any]:
    # protobuf 3.20: message_factory.GetMessages([FileDescriptorProto, ...])
    # строит внутренний пул и возвращает {full_name: message_class}.
    msgs = _mf.GetMessages([_file_descriptor()])
    out: dict[str, Any] = {}
    for name in ("PushDataV3ApiWrapper", "PublicAggreDepthsV3Api",
                 "PublicAggreDepthV3ApiItem", "PublicAggreDealsV3Api",
                 "PublicDealsV3ApiItem"):
        out[name] = msgs[f"{_PACKAGE}.{name}"]
    return out


def wrapper_class() -> Any:
    return _classes()["PushDataV3ApiWrapper"]


def parse_wrapper(data: bytes) -> Any:
    """Распарсить сырые байты WS-сообщения в PushDataV3ApiWrapper."""
    return wrapper_class().FromString(data)
