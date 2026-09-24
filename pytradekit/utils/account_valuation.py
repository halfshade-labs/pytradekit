"""Read-only wallet valuation contracts, independent of trading price selection.

All persisted financial numbers are Decimal-compatible strings. Only USDT is
numeraire; other stablecoins require the same fresh USDT quote as any asset.
"""
import json
import re
from decimal import Decimal, InvalidOperation

from pytradekit.trading_setup.inst_code_usage import (
    convert_base_quote_to_inst_code, convert_pair_to_inst_code,
    convert_symbol_to_inst_code, extract_base_from_inst_code,
)
from pytradekit.utils.dynamic_types import ExchangeId
from pytradekit.utils.time_handler import get_timestamp_ms

SCHEMA_VERSION = 1
SCOPE_VERSION = "bn-okx-wallets-v1"
QUOTE_VALID_MS = 600_000
CLOCK_TOLERANCE_MS = 5_000
COLLECTOR_STALE_MS = 300_000
SNAPSHOT_TTL_S = 86_400
WALLETS = {ExchangeId.BN.name: ("spot", "funding", "usd_m"),
           ExchangeId.OKX.name: ("trading", "funding")}
_ZERO = Decimal("0")


class ValuationError(ValueError):
    """An invalid input cannot be represented as a complete valuation."""


def decimal_value(value):
    if value is None or isinstance(value, bool):
        raise ValuationError("missing numeric value")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValuationError("invalid numeric value") from exc
    if not number.is_finite():
        raise ValuationError("nonfinite numeric value")
    return number


def asset_code(value):
    if (not isinstance(value, str) or not value or value != value.upper()
            or not all(char.isalpha() or char.isdigit() for char in value)):
        raise ValuationError("invalid asset identity")
    return value


def timestamp_ms(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{13}", str(value)):
        raise ValuationError("invalid millisecond timestamp")
    return int(value)


def quote_key(exchange):
    return f"valuation:quotes:v1:{exchange}"


def quote_status_key(exchange):
    return f"valuation:quote_status:v1:{exchange}"


def _quote_items(exchange, response):
    if exchange == ExchangeId.BN.name:
        items = response
    elif exchange == ExchangeId.OKX.name:
        if not isinstance(response, dict) or str(response.get("code")) != "0":
            raise ValuationError("invalid OKX ticker response")
        items = response.get("data")
    elif exchange == ExchangeId.HTX.name:
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise ValuationError("invalid HTX ticker response")
        items = response.get("data")
    else:
        raise ValuationError("unsupported quote venue")
    if not isinstance(items, list) or not items:
        raise ValuationError("empty or invalid ticker batch")
    return items


def _quote_identity(exchange, item):
    key = "instId" if exchange == ExchangeId.OKX.name else "symbol"
    symbol = item.get(key)
    if not isinstance(symbol, str):
        raise ValuationError("invalid ticker identity")
    symbol = symbol.upper()
    parts = symbol.split("-") if key == "instId" else [symbol]
    if len(parts) != (2 if key == "instId" else 1):
        raise ValuationError("invalid ticker identity")
    for part in parts:
        asset_code(part)
    converter = convert_pair_to_inst_code if key == "instId" else convert_symbol_to_inst_code
    code = converter(symbol, exchange_id=exchange)
    base = extract_base_from_inst_code(code)
    expected = convert_base_quote_to_inst_code(base, "USDT", exchange)
    return (base, code) if code == expected else (None, code)


def build_quote_snapshot(exchange, response, received_ms):
    """Replace the whole batch, retaining invalid identities as diagnostics.

    Partial batches publish valid rows but never advance collector last_success.
    An empty/malformed outer response must not replace the prior batch.
    """
    received_ms = timestamp_ms(received_ms)
    quotes, invalid, seen = {}, [], set()
    for item in _quote_items(exchange, response):
        try:
            if not isinstance(item, dict):
                raise ValuationError("invalid ticker row")
            base, code = _quote_identity(exchange, item)
            if code in seen:
                raise ValuationError("duplicate ticker identity")
            seen.add(code)
            if base is None:
                continue
            price_field = {ExchangeId.BN.name: "lastPrice", ExchangeId.OKX.name: "last",
                           ExchangeId.HTX.name: "close"}[exchange]
            price = decimal_value(item.get(price_field))
            event_ms = timestamp_ms(item.get("closeTime") if exchange == ExchangeId.BN.name
                                    else item.get("ts") if exchange == ExchangeId.OKX.name
                                    else response.get("ts"))
            if price <= 0 or not -CLOCK_TOLERANCE_MS <= received_ms - event_ms <= QUOTE_VALID_MS:
                raise ValuationError("invalid price or quote time")
            quotes[base] = {"asset": base, "exchange": exchange, "inst_code": code,
                            "price": str(price), "exchange_ms": event_ms,
                            "received_ms": received_ms}
        except ValuationError as exc:
            # Remove duplicate identities too: neither copy is authoritative.
            if isinstance(item, dict):
                try:
                    base, _ = _quote_identity(exchange, item)
                    quotes.pop(base, None)
                except ValuationError:
                    pass
            invalid.append(str(exc))
    if not quotes and not invalid:
        raise ValuationError("batch has no USDT quotes")
    return {"schema_version": SCHEMA_VERSION, "exchange": exchange,
            "received_ms": received_ms, "quotes": quotes, "invalid": invalid,
            "status": "complete" if quotes and not invalid else "partial"}


def select_quote(asset, exchange, context):
    """Select own-venue then BN price from full snapshots at context.now_ms."""
    asset_code(asset)
    now = timestamp_ms(context["now_ms"])
    if asset == "USDT":
        return {"asset": asset, "price": "1", "exchange": exchange,
                "exchange_ms": now, "received_ms": now, "kind": "numeraire"}
    for venue in dict.fromkeys((exchange, ExchangeId.BN.name)):
        batch = context["quotes"].get(venue)
        if not isinstance(batch, dict) or batch.get("schema_version") != SCHEMA_VERSION:
            continue
        if batch.get("exchange") != venue or not isinstance(batch.get("quotes"), dict):
            continue
        quote = batch["quotes"].get(asset)
        try:
            expected = convert_base_quote_to_inst_code(asset, "USDT", venue)
            if (not isinstance(quote, dict) or quote.get("inst_code") != expected
                    or quote.get("asset") != asset or quote.get("exchange") != venue):
                continue
            event_ms, received_ms = timestamp_ms(quote["exchange_ms"]), timestamp_ms(quote["received_ms"])
            if not all(-CLOCK_TOLERANCE_MS <= now - ts <= QUOTE_VALID_MS
                       for ts in (event_ms, received_ms)):
                continue
            if event_ms - received_ms > CLOCK_TOLERANCE_MS or decimal_value(quote["price"]) <= 0:
                continue
            return dict(quote, kind="own_venue" if venue == exchange else "bn_fallback")
        except (KeyError, ValuationError):
            continue
    return None


def load_quote_snapshots(redis):
    snapshots = {}
    for exchange in (ExchangeId.BN.name, ExchangeId.OKX.name, ExchangeId.HTX.name):
        try:
            snapshots[exchange] = redis.get_json(quote_key(exchange))
        except Exception:
            snapshots[exchange] = None
    return snapshots


def _rows(raw, field):
    rows = raw.get(field) if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise ValuationError("invalid wallet response")
    return rows


def _okx_rows(raw, wallet):
    if not isinstance(raw, dict) or str(raw.get("code")) != "0":
        raise ValuationError("invalid OKX wallet response")
    data = _rows(raw, "data")
    if wallet == "trading":
        if len(data) != 1:
            raise ValuationError("ambiguous OKX account response")
        return _rows(data[0], "details")
    return data


def _optional_native(row, field):
    value = row.get(field)
    return _ZERO if value in (None, "") else decimal_value(value)


def _normalize_bn(row, wallet):
    asset = asset_code(row.get("asset"))
    if wallet == "usd_m":
        quantity = decimal_value(row.get("walletBalance"))
        equity = decimal_value(row.get("marginBalance"))
        upl = decimal_value(row.get("unrealizedProfit"))
        if equity != quantity + upl:
            raise ValuationError("inconsistent BN native equity")
        return asset, quantity, equity, _ZERO, upl, {}
    fields = ("free", "locked") if wallet == "spot" else ("free", "locked", "freeze", "withdrawing")
    amounts = {key: decimal_value(row.get(key)) for key in fields}
    quantity = sum(amounts.values(), _ZERO)
    return asset, quantity, quantity, _ZERO, _ZERO, amounts


def _normalize_okx(row, wallet):
    asset = asset_code(row.get("ccy"))
    if wallet == "trading":
        quantity, equity = decimal_value(row.get("cashBal")), decimal_value(row.get("eq"))
        liabilities, upl = _optional_native(row, "liab"), _optional_native(row, "upl")
    else:
        quantity = equity = decimal_value(row.get("bal"))
        liabilities = upl = _ZERO
    native = {key: _optional_native(row, key) for key in ("availBal", "frozenBal")}
    return asset, quantity, equity, liabilities, upl, native


def normalize_wallet(exchange, wallet, raw):
    if exchange not in WALLETS or wallet not in WALLETS[exchange]:
        raise ValuationError("unsupported wallet scope")
    if exchange == ExchangeId.OKX.name:
        rows = _okx_rows(raw, wallet)
    elif wallet == "spot":
        rows = _rows(raw, "balances")
    elif wallet == "usd_m":
        if not isinstance(raw, dict) or type(raw.get("multiAssetsMargin")) is not bool:
            raise ValuationError("unsupported BN account mode")
        rows = _rows(raw, "assets")
    else:
        rows = raw
    if not isinstance(rows, list):
        raise ValuationError("invalid wallet rows")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValuationError("invalid wallet row")
        normalized = _normalize_bn(row, wallet) if exchange == ExchangeId.BN.name else _normalize_okx(row, wallet)
        asset, quantity, equity, liabilities, upl, native = normalized
        if asset in seen:
            raise ValuationError("duplicate wallet asset")
        seen.add(asset)
        if any(value != 0 for value in (quantity, equity, liabilities, upl)):
            result.append({"exchange": exchange, "wallet": wallet, "asset": asset,
                           "quantity": str(quantity), "equity": str(equity),
                           "liabilities": str(liabilities), "unrealized_pnl": str(upl),
                           "native": {key: str(value) for key, value in native.items()}})
    return result


def collect_wallets(exchange, account_id, fetchers):
    """Capture each declared wallet, with sanitized failure states and no orders."""
    if exchange not in WALLETS or not isinstance(account_id, str) or not account_id:
        raise ValuationError("invalid account scope")
    if set(fetchers) != set(WALLETS[exchange]):
        raise ValuationError("incomplete declared wallet scope")
    start, rows, sources = get_timestamp_ms(), [], []
    for wallet in WALLETS[exchange]:
        source = {"wallet": wallet, "capture_start_ms": get_timestamp_ms()}
        try:
            wallet_rows = normalize_wallet(exchange, wallet, fetchers[wallet]())
            rows.extend(dict(row, account_id=account_id) for row in wallet_rows)
            source.update(status="complete", error=None)
        except Exception:
            source.update(status="unavailable", error="wallet_fetch_or_validation_failed")
        source["capture_end_ms"] = get_timestamp_ms()
        sources.append(source)
    return {"schema_version": SCHEMA_VERSION, "scope_version": SCOPE_VERSION,
            "exchange": exchange, "account_id": account_id,
            "wallets": list(WALLETS[exchange]), "capture_start_ms": start,
            "capture_end_ms": get_timestamp_ms(), "sources": sources, "rows": rows}


def value_wallets(snapshot, quote_context):
    result, rows, missing = dict(snapshot), [], []
    visible_total = _ZERO
    for original in snapshot["rows"]:
        row = dict(original)
        quote = select_quote(row["asset"], row["exchange"], quote_context)
        equity = decimal_value(row["equity"])
        row.update(quote=quote, price=quote["price"] if quote else None,
                   value=str(equity * decimal_value(quote["price"])) if quote else None)
        if quote:
            visible_total += decimal_value(row["value"])
        else:
            missing.append(f"{row['asset']}@{row['exchange']}:{row['wallet']}")
        rows.append(row)
    complete = (not missing and all(s["status"] == "complete" for s in snapshot["sources"])
                and 0 <= snapshot["capture_end_ms"] - snapshot["capture_start_ms"] <= 900_000)
    result.update(rows=rows, missing=missing, status="complete" if complete else "partial",
                  visible_total=str(visible_total), total_equity=str(visible_total) if complete else None,
                  valuation_ms=quote_context["now_ms"], quote_sources=quote_context.get("sources", {}))
    return result


def legacy_balance_value(snapshot):
    """Numeric-compatible aggregate equity for old reports, never exposure.

    The version/status fields survive read_balance_df, whose API drops `other`.
    This permits legacy reports to reject partial/cross-scope summaries explicitly.
    """
    result = {}
    for row in snapshot["rows"]:
        value = result.setdefault(row["asset"], {"volume": _ZERO, "price": row["price"],
                                                "value_in_u": _ZERO})
        value["volume"] += decimal_value(row["equity"])
        if value["value_in_u"] is None or row["value"] is None:
            value["price"] = value["value_in_u"] = None
        else:
            value["value_in_u"] += decimal_value(row["value"])
    if not result:
        result["USDT"] = {"volume": _ZERO, "price": "1", "value_in_u": _ZERO}
    for values in result.values():
        values.update(scope_version=SCOPE_VERSION, valuation_status=snapshot["status"])
        if snapshot["status"] != "complete":
            values["value_in_u"] = None
        for key in ("volume", "price", "value_in_u"):
            values[key] = str(values[key]) if values[key] is not None else None
    return result


def load_quote_statuses(redis, now_ms):
    statuses = {}
    for exchange in (ExchangeId.BN.name, ExchangeId.OKX.name, ExchangeId.HTX.name):
        try:
            state = dict(redis.get_json(quote_status_key(exchange)) or {})
            last_success = state.get("last_success_ms")
            age = now_ms - timestamp_ms(last_success) if last_success is not None else None
            state["age_ms"] = age
            if age is None or age < -CLOCK_TOLERANCE_MS or age > COLLECTOR_STALE_MS:
                state["status"] = "stale"
            statuses[exchange] = state
        except Exception:
            statuses[exchange] = {"status": "unavailable", "error": "quote_status_unavailable"}
    return statuses


def serialize_legacy_balance(snapshot):
    """Emit exact JSON numbers for existing consumers without a float round-trip."""
    result = legacy_balance_value(snapshot)
    numeric = {"volume", "price", "value_in_u"}
    return "{" + ",".join(
        json.dumps(asset) + ":{" + ",".join(
            json.dumps(key) + ":" + (
                "null" if value is None else str(decimal_value(value))
                if key in numeric else json.dumps(value)
            ) for key, value in fields.items()
        ) + "}" for asset, fields in result.items()
    ) + "}"


def read_cash_inventory(snapshot, scope, now_ms):
    """Strict adapter for legacy read-only monitors; unknown is never empty."""
    exchange, account_id = scope
    if (snapshot.get("schema_version") != SCHEMA_VERSION
            or snapshot.get("scope_version") != SCOPE_VERSION
            or snapshot.get("exchange") != exchange or snapshot.get("account_id") != account_id
            or snapshot.get("wallets") != list(WALLETS[exchange])
            or snapshot.get("status") != "complete"):
        raise ValuationError("incomplete account valuation scope")
    start, end = timestamp_ms(snapshot.get("capture_start_ms")), timestamp_ms(snapshot.get("capture_end_ms"))
    if not (0 <= end - start <= 900_000 and 0 <= now_ms - end <= 10_800_000):
        raise ValuationError("stale or skewed account capture")
    sources = snapshot.get("sources")
    if (not isinstance(sources, list) or [s.get("wallet") for s in sources] != list(WALLETS[exchange])
            or any(s.get("status") != "complete" for s in sources)):
        raise ValuationError("incomplete account wallets")
    result, seen = {}, set()
    for row in snapshot["rows"]:
        identity = (row["wallet"], asset_code(row["asset"]))
        if (identity in seen or row["wallet"] not in WALLETS[exchange]
                or row["account_id"] != account_id or row["exchange"] != exchange):
            raise ValuationError("invalid wallet row scope")
        seen.add(identity)
        attached = row.get("quote") or {}
        venue = attached.get("exchange", exchange)
        quote = select_quote(row["asset"], exchange, {"now_ms": now_ms, "quotes": {
            venue: {"schema_version": SCHEMA_VERSION, "exchange": venue, "quotes": {row["asset"]: attached}}}})
        if quote is None:
            raise ValuationError("missing or stale wallet quote")
        if row["wallet"] == "usd_m":
            continue
        item = result.setdefault(row["asset"], {"quantity": _ZERO, "price": decimal_value(quote["price"])})
        item["quantity"] += decimal_value(row["quantity"])
    return result
