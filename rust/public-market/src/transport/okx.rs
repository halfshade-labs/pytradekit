//! OKX public spot tickers protocol.
use super::{
    spot_request, validate_instruments as validate_targets, SpotInstruments, SpotQuote,
    SpotReception, Wire,
};
use crate::Result;
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    net::SocketAddr,
    time::{Duration, Instant},
};
use tungstenite::{handshake::client::Request, Message};

const CHANNEL: &str = "tickers";
const PING_AFTER: Duration = Duration::from_secs(20);
const PONG_TIMEOUT: Duration = Duration::from_secs(5);
// Every valid ticker row has at least four keys and is longer than 32 wire bytes.
pub(super) const MAX_TICKER_ROWS: usize = crate::MAX_LINE / 32;

pub(super) fn request(address: SocketAddr, uri: &str) -> Result<Request> {
    spot_request(address, uri, |uri| {
        uri.host() == Some("ws.okx.com")
            && uri.port_u16().unwrap_or(443) == 443
            && address.port() == 443
            && uri.path() == "/ws/v5/public"
    })
}

pub(super) fn validate_instruments(instruments: &SpotInstruments) -> Result<()> {
    validate_targets(instruments, |symbol, instrument| {
        instrument.split_once('-').is_some_and(|(base, quote)| {
            crate::symbol_valid(base)
                && crate::symbol_valid(quote)
                && symbol.bytes().eq(base.bytes().chain(quote.bytes()))
        })
    })
}

pub(super) struct Socket {
    wire: Wire,
    protocol: Protocol,
    heartbeat: Heartbeat,
}
impl Socket {
    pub(super) fn connect(
        address: SocketAddr,
        uri: &str,
        instruments: &SpotInstruments,
    ) -> Result<Self> {
        let mut wire = Wire::connect(address, request(address, uri)?, None)?;
        let protocol = Protocol::new(instruments);
        let args: Vec<_> = protocol
            .pending
            .iter()
            .map(|instrument| json!({"channel": CHANNEL, "instId": instrument}))
            .collect();
        wire.send(|| {
            Ok(Message::Text(
                json!({"op": "subscribe", "args": args}).to_string().into(),
            ))
        })?;
        Ok(Self {
            wire,
            protocol,
            heartbeat: Heartbeat::new(Instant::now()),
        })
    }

    pub(super) fn receive(&mut self, receive_ms: impl FnOnce() -> i64) -> Result<SpotReception> {
        if self.heartbeat.check(Instant::now())? {
            self.wire.send(|| Ok(Message::Text("ping".into())))?;
        }
        match self.wire.read()? {
            Some(Message::Text(raw)) => {
                let received = receive_ms();
                let now = Instant::now();
                if raw.as_str() == "pong" {
                    self.heartbeat.pong(now);
                    return Ok(SpotReception::Idle);
                }
                let value = crate::json::parse(raw.as_bytes())?;
                self.heartbeat.activity(now);
                self.protocol.decode(&value, received)
            }
            Some(Message::Close(_)) => Err("OKX physical connection closed".into()),
            None | Some(Message::Ping(_) | Message::Pong(_)) => Ok(SpotReception::Idle),
            _ => Err("unsupported OKX public frame".into()),
        }
    }

    pub(super) fn close(mut self) -> Result<()> {
        self.wire.close()
    }
}

struct Heartbeat {
    last_activity: Instant,
    pending: Option<Instant>,
}
impl Heartbeat {
    fn new(now: Instant) -> Self {
        Self {
            last_activity: now,
            pending: None,
        }
    }

    fn check(&mut self, now: Instant) -> Result<bool> {
        if let Some(sent) = self.pending {
            if now.duration_since(sent) >= PONG_TIMEOUT {
                return Err("OKX text pong timeout".into());
            }
        } else if now.duration_since(self.last_activity) >= PING_AFTER {
            self.pending = Some(now);
            return Ok(true);
        }
        Ok(false)
    }

    fn activity(&mut self, now: Instant) {
        self.last_activity = now;
    }

    fn pong(&mut self, now: Instant) {
        if self.pending.take().is_some() {
            self.activity(now);
        }
    }
}

struct Protocol {
    pending: BTreeSet<String>,
    bindings: BTreeMap<String, String>,
}
impl Protocol {
    fn new(instruments: &SpotInstruments) -> Self {
        let bindings: BTreeMap<_, _> = instruments
            .iter()
            .map(|(symbol, instrument)| (instrument.clone(), symbol.clone()))
            .collect();
        Self {
            pending: bindings.keys().cloned().collect(),
            bindings,
        }
    }

    fn decode(&mut self, value: &Value, receive_ms: i64) -> Result<SpotReception> {
        let arg = &value["arg"];
        let instrument = arg["instId"].as_str().filter(|_| arg["channel"] == CHANNEL);
        if let Some(event) = value.get("event") {
            if event != "subscribe"
                || !instrument.is_some_and(|instrument| self.pending.remove(instrument))
            {
                return Err("invalid OKX subscription ACK or session event".into());
            }
            return Ok(if self.pending.is_empty() {
                SpotReception::Acknowledged
            } else {
                SpotReception::Idle
            });
        }
        let Some((instrument, symbol)) = instrument.and_then(|instrument| {
            self.bindings
                .get(instrument)
                .filter(|_| !self.pending.contains(instrument))
                .map(|symbol| (instrument, symbol))
        }) else {
            return Ok(SpotReception::Idle);
        };
        let quotes: Vec<_> = value["data"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|row| quote(row, instrument, symbol, receive_ms))
            .take(MAX_TICKER_ROWS + 1)
            .collect();
        if quotes.len() > MAX_TICKER_ROWS {
            return Err("OKX ticker row limit".into());
        }
        Ok(if quotes.is_empty() {
            SpotReception::Idle
        } else {
            SpotReception::Quotes(quotes)
        })
    }
}

fn quote(row: &Value, instrument: &str, symbol: &str, receive_ms: i64) -> Option<SpotQuote> {
    if row["instId"] != instrument || row.get("instType").is_some_and(|kind| kind != "SPOT") {
        return None;
    }
    let source = row["ts"].as_str()?;
    if source.is_empty() || !source.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let sequence = match row.get("seqId") {
        None | Some(Value::Null) => None,
        Some(value) => Some(value.as_i64().filter(|value| *value >= 0)?),
    };
    Some(SpotQuote {
        symbol: symbol.into(),
        bid: row["bidPx"]
            .as_str()
            .filter(|price| price.len() <= super::MAX_SPOT_PRICE_TEXT)?
            .into(),
        ask: row["askPx"]
            .as_str()
            .filter(|price| price.len() <= super::MAX_SPOT_PRICE_TEXT)?
            .into(),
        source_ms: source.parse().ok().filter(|time| *time > 0)?,
        receive_ms,
        sequence,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn okx_fixture_orders_rows_and_checks_channel_instrument_and_ack() {
        let mut protocol = Protocol::new(&BTreeMap::from([
            ("BTCUSDT".into(), "BTC-USDT".into()),
            ("ETHUSDT".into(), "ETH-USDT".into()),
        ]));
        let input = json!({"arg":{"channel":"tickers","instId":"BTC-USDT"},"data":[
            {"instType":"SPOT","instId":"BTC-USDT","bidPx":"0.100000000000000000001","askPx":"0.2","ts":"1630994555539"},
            {"instType":"SPOT","instId":"ETH-USDT","bidPx":"3","askPx":"4","ts":"1630994555538"},
            {"instType":"SPOT","instId":"BTC-USDT","bidPx":0.1,"askPx":"0.2","ts":"1630994555540"},
            {"instType":"SPOT","instId":"BTC-USDT","bidPx":"0.2","askPx":"0.3","ts":"1630994555540","seqId":0}
        ]});
        let ack =
            |channel, inst| json!({"event":"subscribe","arg":{"channel":channel,"instId":inst}});
        assert!(protocol.decode(&ack("books", "BTC-USDT"), 1).is_err());
        assert!(protocol.decode(&ack(CHANNEL, "BAD-USDT"), 1).is_err());
        assert!(matches!(
            protocol.decode(&input, 1),
            Ok(SpotReception::Idle)
        ));
        assert!(matches!(
            protocol.decode(&ack(CHANNEL, "BTC-USDT"), 1),
            Ok(SpotReception::Idle)
        ));
        assert!(matches!(protocol.decode(&input, 1),
            Ok(SpotReception::Quotes(quotes)) if quotes.len() == 2));
        assert!(matches!(
            protocol.decode(&ack(CHANNEL, "ETH-USDT"), 1),
            Ok(SpotReception::Acknowledged)
        ));
        let mut wrong = input.clone();
        wrong["arg"]["channel"] = json!("books");
        assert!(matches!(
            protocol.decode(&wrong, 1),
            Ok(SpotReception::Idle)
        ));
        let SpotReception::Quotes(quotes) = protocol.decode(&input, 1).unwrap() else {
            panic!("expected ordered OKX rows")
        };
        assert_eq!(quotes.len(), 2);
        assert_eq!(
            (
                &quotes[0].bid[..],
                quotes[0].source_ms,
                quotes[0].receive_ms,
                quotes[0].sequence
            ),
            ("0.100000000000000000001", 1_630_994_555_539, 1, None)
        );
        assert_eq!((&quotes[1].bid[..], quotes[1].sequence), ("0.2", Some(0)));
    }

    #[test]
    fn okx_actual_socket_text_ping_pong_and_missing_pong_timeout() {
        use std::{net::TcpListener, sync::mpsc, thread};

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let limit = Duration::from_secs(4);
        let (finished, hold) = mpsc::channel();
        thread::scope(|scope| {
            let peer = scope.spawn(move || {
                let until = Instant::now() + limit;
                let stream = loop {
                    assert!(Instant::now() < until, "no OKX upgrade");
                    if let Ok((stream, _)) = listener.accept() {
                        break stream;
                    }
                    thread::sleep(Duration::from_millis(1));
                };
                stream.set_read_timeout(Some(limit)).unwrap();
                stream.set_write_timeout(Some(limit)).unwrap();
                let mut socket = tungstenite::accept(stream).unwrap();
                let subscribe: Value = serde_json::from_str(&socket.read().unwrap().into_text().unwrap()).unwrap();
                let arg = json!({"channel": "tickers", "instId": "BTC-USDT"});
                assert_eq!(subscribe, json!({"op": "subscribe", "args": [arg]}));
                socket.send(Message::Text(json!({"event":"subscribe","arg":arg}).to_string().into())).unwrap();
                assert_eq!(socket.read().unwrap(), Message::Text("ping".into()));
                socket.send(Message::Text("pong".into())).unwrap();
                assert_eq!(socket.read().unwrap(), Message::Text("ping".into()));
                // Market activity is observed while the second text pong remains outstanding.
                let tick = json!({"arg":arg,"data":[{"instId":"BTC-USDT","bidPx":"1","askPx":"2","ts":"1"}]});
                socket.send(Message::Text(tick.to_string().into())).unwrap();
                let _ = hold.recv_timeout(limit);
            });
            let instruments = BTreeMap::from([("BTCUSDT".into(), "BTC-USDT".into())]);
            let mut socket =
                Socket::connect(address, &format!("ws://{address}/fixture"), &instruments).unwrap();
            let until = Instant::now() + limit;
            while !matches!(socket.receive(|| 1).unwrap(), SpotReception::Acknowledged) {
                assert!(Instant::now() < until, "no OKX ACK");
            }

            socket.heartbeat.last_activity = Instant::now() - PING_AFTER;
            assert!(matches!(socket.receive(|| 1).unwrap(), SpotReception::Idle));
            while socket.heartbeat.pending.is_some() {
                assert!(Instant::now() < until, "no OKX text pong");
                socket.receive(|| 1).unwrap();
            }

            socket.heartbeat.last_activity = Instant::now() - PING_AFTER;
            while !matches!(socket.receive(|| 1).unwrap(), SpotReception::Quotes(_)) {
                assert!(
                    Instant::now() < until,
                    "no OKX ticker while pong is pending"
                );
            }
            assert!(socket.heartbeat.pending.is_some());
            socket.heartbeat.pending = Some(Instant::now() - PONG_TIMEOUT);
            assert_eq!(
                socket.receive(|| 1).err().as_deref(),
                Some("OKX text pong timeout")
            );
            finished.send(()).unwrap();
            peer.join().unwrap();
        });
    }

    #[test]
    fn okx_public_endpoint_policy_and_symbol_limit() {
        let remote = "192.0.2.1:443".parse().unwrap();
        let old_port = "192.0.2.1:8443".parse().unwrap();
        let loopback = "127.0.0.1:8443".parse().unwrap();
        assert!(request(remote, "wss://ws.okx.com/ws/v5/public").is_ok());
        assert!(request(remote, "wss://ws.okx.com:443/ws/v5/public").is_ok());
        assert!(request(remote, "wss://ws.okx.com:8443/ws/v5/public").is_err());
        assert!(request(old_port, "wss://ws.okx.com/ws/v5/public").is_err());
        assert!(request(remote, "wss://ws.okx.com/ws/v5/private").is_err());
        assert!(request(remote, "wss://ws.okx.com:9001/ws/v5/public").is_err());
        assert!(request(loopback, "ws://localhost:8443/test").is_ok());
        let targets: SpotInstruments = (0..=crate::MAX_SYMBOLS)
            .map(|index| (format!("T{index}USDT"), format!("T{index}-USDT")))
            .collect();
        assert!(validate_instruments(&targets).is_err());
        assert!(
            validate_instruments(&BTreeMap::from([("T1USDT".into(), "T1-USDT-SWAP".into())]))
                .is_err()
        );
    }

    #[test]
    fn okx_target_requires_exact_concatenated_spot_instrument() {
        let binding = |wire: &str| BTreeMap::from([("ETHBTC".into(), wire.into())]);
        assert!(validate_instruments(&binding("ETH-BTC")).is_ok());
        for invalid in [
            "BTC-ETH",
            "ETH-USDT",
            "eth-btc",
            "ETH--BTC",
            "ETHBTC",
            "ETH-BTC-SWAP",
        ] {
            assert!(
                validate_instruments(&binding(invalid)).is_err(),
                "{invalid}"
            );
        }
    }

    #[test]
    fn okx_ticker_batch_has_a_decoded_row_bound() {
        let mut protocol = Protocol::new(&BTreeMap::from([("BTCUSDT".into(), "BTC-USDT".into())]));
        let arg = json!({"channel":"tickers","instId":"BTC-USDT"});
        let ack = json!({"event":"subscribe","arg":arg});
        assert!(matches!(
            protocol.decode(&ack, 1),
            Ok(SpotReception::Acknowledged)
        ));
        let row = json!({"instId":"BTC-USDT","bidPx":"1","askPx":"2","ts":"1"});
        let frame = |rows| json!({"arg":arg,"data":vec![row.clone(); rows]});
        assert!(
            matches!(protocol.decode(&frame(MAX_TICKER_ROWS), 1), Ok(SpotReception::Quotes(quotes)) if quotes.len()==MAX_TICKER_ROWS)
        );
        assert_eq!(
            protocol
                .decode(&frame(MAX_TICKER_ROWS + 1), 1)
                .err()
                .as_deref(),
            Some("OKX ticker row limit")
        );
    }
}
