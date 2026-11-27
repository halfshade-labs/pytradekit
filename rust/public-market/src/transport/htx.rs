//! HTX public spot BBO protocol.
use super::{
    spot_request, validate_instruments as validate_targets, SpotInstruments, SpotQuote,
    SpotReception, Wire,
};
use crate::{Result, MAX_LINE};
use flate2::bufread::GzDecoder;
use serde::de::{Error, MapAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{json, value::RawValue};
use std::{
    collections::BTreeMap,
    fmt,
    io::Read,
    net::SocketAddr,
    time::{Duration, Instant},
};
use tungstenite::{handshake::client::Request, Message};

const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(3 * 5);

pub(super) fn request(address: SocketAddr, uri: &str) -> Result<Request> {
    spot_request(address, uri, |uri| {
        matches!(uri.host(), Some("api.huobi.pro" | "api-aws.huobi.pro"))
            && uri.port_u16().unwrap_or(443) == 443
            && uri.path() == "/ws"
    })
}

pub(super) fn validate_instruments(instruments: &SpotInstruments) -> Result<()> {
    validate_targets(instruments, |symbol, instrument| {
        instrument
            .bytes()
            .eq(symbol.bytes().map(|byte| byte.to_ascii_lowercase()))
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
        for (channel, id) in &protocol.pending {
            wire.send(|| {
                Ok(Message::Text(
                    json!({"sub": channel, "id": id}).to_string().into(),
                ))
            })?;
        }
        Ok(Self {
            wire,
            protocol,
            heartbeat: Heartbeat::default(),
        })
    }

    pub(super) fn receive(&mut self, receive_ms: impl FnOnce() -> i64) -> Result<SpotReception> {
        self.heartbeat.check(Instant::now())?;
        let message = self.wire.read()?;
        let now = Instant::now();
        self.heartbeat.check(now)?;
        match message {
            Some(Message::Binary(bytes)) => {
                let received = receive_ms();
                match self.protocol.decode(&inflate(&bytes)?, received)? {
                    Packet::Pong(value) => {
                        self.wire.send(|| {
                            Ok(Message::Text(json!({"pong": value}).to_string().into()))
                        })?;
                        self.heartbeat.ping(now);
                        Ok(SpotReception::Idle)
                    }
                    Packet::Reception(SpotReception::Acknowledged) => {
                        self.heartbeat.acknowledged(now);
                        Ok(SpotReception::Acknowledged)
                    }
                    Packet::Reception(reception) => Ok(reception),
                }
            }
            Some(Message::Close(_)) => Err("HTX physical connection closed".into()),
            None | Some(Message::Ping(_) | Message::Pong(_)) => Ok(SpotReception::Idle),
            _ => Err("unsupported HTX public frame".into()),
        }
    }

    pub(super) fn close(mut self) -> Result<()> {
        self.wire.close()
    }
}

#[derive(Default)]
struct Heartbeat {
    last_ping: Option<Instant>,
}

impl Heartbeat {
    fn acknowledged(&mut self, now: Instant) {
        self.last_ping = Some(now);
    }

    fn ping(&mut self, now: Instant) {
        if let Some(last_ping) = &mut self.last_ping {
            *last_ping = now;
        }
    }

    fn check(&self, now: Instant) -> Result<()> {
        if self
            .last_ping
            .is_some_and(|last_ping| now.duration_since(last_ping) >= HEARTBEAT_TIMEOUT)
        {
            return Err("HTX application heartbeat timeout".into());
        }
        Ok(())
    }
}

fn inflate(compressed: &[u8]) -> Result<Vec<u8>> {
    if compressed.len() > MAX_LINE {
        return Err("HTX compressed frame limit".into());
    }
    let mut decoded = Vec::new();
    let mut gzip = GzDecoder::new(compressed).take((MAX_LINE + 1) as u64);
    gzip.read_to_end(&mut decoded)
        .map_err(|_| "invalid HTX gzip frame")?;
    if decoded.len() > MAX_LINE {
        return Err("HTX inflated frame limit".into());
    }
    if !gzip.into_inner().into_inner().is_empty() {
        return Err("trailing data in HTX gzip frame".into());
    }
    Ok(decoded)
}

struct Binding {
    symbol: String,
    instrument: String,
}

struct Protocol {
    pending: BTreeMap<String, String>,
    bindings: BTreeMap<String, Binding>,
}

enum Packet {
    Pong(i64),
    Reception(SpotReception),
}

impl Protocol {
    fn new(instruments: &SpotInstruments) -> Self {
        let bindings = instruments
            .iter()
            .map(|(symbol, instrument)| {
                (
                    format!("market.{instrument}.bbo"),
                    Binding {
                        symbol: symbol.clone(),
                        instrument: instrument.clone(),
                    },
                )
            })
            .collect::<BTreeMap<_, _>>();
        let pending = bindings
            .keys()
            .enumerate()
            .map(|(index, channel)| (channel.clone(), format!("bbo{}", index + 1)))
            .collect();
        Self { pending, bindings }
    }

    fn decode(&mut self, bytes: &[u8], receive_ms: i64) -> Result<Packet> {
        let object: RawObject<'_> =
            serde_json::from_slice(bytes).map_err(|error| error.to_string())?;
        object.check_nested(0)?;
        if object.contains("ping") {
            return Ok(Packet::Pong(
                object.integer("ping").ok_or("invalid HTX ping")?,
            ));
        }
        if object.contains("status") || object.contains("subbed") || object.contains("id") {
            let channel = object.string("subbed").ok_or("invalid HTX ACK channel")?;
            let id = object.string("id").ok_or("invalid HTX ACK ID")?;
            if object.string("status").as_deref() != Some("ok")
                || self.pending.get(&channel) != Some(&id)
            {
                return Err("invalid HTX subscription ACK".into());
            }
            self.pending.remove(&channel);
            return Ok(Packet::Reception(if self.pending.is_empty() {
                SpotReception::Acknowledged
            } else {
                SpotReception::Idle
            }));
        }
        let quote = self.quote(&object, receive_ms);
        Ok(Packet::Reception(match quote {
            Some(quote) => SpotReception::Quotes(vec![quote]),
            None => SpotReception::Idle,
        }))
    }

    fn quote(&self, object: &RawObject<'_>, receive_ms: i64) -> Option<SpotQuote> {
        let channel = object.string("ch")?;
        if self.pending.contains_key(&channel) {
            return None;
        }
        let binding = self.bindings.get(&channel)?;
        let tick: RawObject<'_> = serde_json::from_str(object.raw("tick")?).ok()?;
        if tick.string("symbol")? != binding.instrument {
            return None;
        }
        let sequence = match tick.optional_sequence("seqId")? {
            None | Some(0) => tick.optional_sequence("version")?,
            primary => primary,
        };
        Some(SpotQuote {
            symbol: binding.symbol.clone(),
            bid: tick.number("bid")?.into(),
            ask: tick.number("ask")?.into(),
            source_ms: tick.integer("quoteTime").filter(|time| *time > 0)?,
            receive_ms,
            sequence,
        })
    }
}

// RawValue captures JSON number lexemes without a float round-trip. Its
// children still need a duplicate-key check, including ignored wire fields.
struct RawObject<'a>(BTreeMap<String, &'a RawValue>);
impl<'de> Deserialize<'de> for RawObject<'de> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct ObjectVisitor;
        impl<'de> Visitor<'de> for ObjectVisitor {
            type Value = RawObject<'de>;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("an object without duplicate fields")
            }

            fn visit_map<A: MapAccess<'de>>(
                self,
                mut access: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut fields = BTreeMap::new();
                while let Some((key, raw)) = access.next_entry()? {
                    if fields.insert(key, raw).is_some() {
                        return Err(A::Error::custom("duplicate HTX JSON field"));
                    }
                }
                Ok(RawObject(fields))
            }
        }
        deserializer.deserialize_map(ObjectVisitor)
    }
}
impl<'a> RawObject<'a> {
    fn raw(&self, key: &str) -> Option<&'a str> {
        self.0.get(key).map(|value| value.get())
    }

    fn contains(&self, key: &str) -> bool {
        self.0.contains_key(key)
    }

    fn string(&self, key: &str) -> Option<String> {
        serde_json::from_str(self.raw(key)?).ok()
    }

    fn integer(&self, key: &str) -> Option<i64> {
        self.raw(key)?.parse().ok()
    }

    fn number(&self, key: &str) -> Option<&'a str> {
        self.raw(key).filter(|value| {
            value.len() <= super::MAX_SPOT_PRICE_TEXT
                && matches!(value.as_bytes().first(), Some(b'-' | b'0'..=b'9'))
        })
    }

    fn optional_sequence(&self, key: &str) -> Option<Option<i64>> {
        match self.raw(key) {
            None | Some("null") => Some(None),
            Some(_) => self.integer(key).filter(|value| *value >= 0).map(Some),
        }
    }

    fn check_nested(&self, depth: usize) -> Result<()> {
        for raw in self.0.values() {
            check_nested(raw, depth + 1)?;
        }
        Ok(())
    }
}

fn check_nested(raw: &RawValue, depth: usize) -> Result<()> {
    if depth > 32 {
        return Err("HTX JSON nesting limit".into());
    }
    match raw.get().as_bytes().first() {
        Some(b'{') => serde_json::from_str::<RawObject<'_>>(raw.get())
            .map_err(|error| error.to_string())?
            .check_nested(depth),
        Some(b'[') => {
            let items: Vec<&RawValue> =
                serde_json::from_str(raw.get()).map_err(|error| error.to_string())?;
            for item in items {
                check_nested(item, depth + 1)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::{write::GzEncoder, Compression};
    use std::io::Write;

    fn fixture() -> Protocol {
        Protocol::new(&BTreeMap::from([("BTCUSDT".into(), "btcusdt".into())]))
    }

    fn gzip(raw: &[u8]) -> Vec<u8> {
        let mut writer = GzEncoder::new(Vec::new(), Compression::fast());
        writer.write_all(raw).unwrap();
        writer.finish().unwrap()
    }

    #[test]
    fn htx_fixture_ack_ping_and_exact_quote() {
        const RECEIVE: i64 = 1_630_994_555_540;
        let mut protocol = fixture();
        let quote = br#"{"ch":"market.btcusdt.bbo","ts":1630994555540,"tick":{"seqId":137005210233,"ask":52665.020000000000000001,"bid":5.266501e4,"quoteTime":1630994555539,"symbol":"btcusdt"}}"#;
        assert!(matches!(
            protocol.decode(quote, RECEIVE),
            Ok(Packet::Reception(SpotReception::Idle))
        ));
        assert!(matches!(
            protocol.decode(br#"{"ping":9007199254740993}"#, RECEIVE),
            Ok(Packet::Pong(9_007_199_254_740_993))
        ));
        assert!(protocol.decode(br#"{"ping":1.0}"#, RECEIVE).is_err());
        assert!(protocol
            .decode(
                br#"{"status":"ok","subbed":"market.btcusdt.bbo","id":"wrong"}"#,
                RECEIVE
            )
            .is_err());
        assert!(protocol
            .decode(
                br#"{"status":"ok","subbed":"market.ethusdt.bbo","id":"bbo1"}"#,
                RECEIVE
            )
            .is_err());
        assert!(matches!(
            protocol.decode(
                br#"{"status":"ok","subbed":"market.btcusdt.bbo","id":"bbo1"}"#,
                RECEIVE
            ),
            Ok(Packet::Reception(SpotReception::Acknowledged))
        ));
        let decoded = inflate(&gzip(quote)).unwrap();
        let Packet::Reception(SpotReception::Quotes(quotes)) =
            protocol.decode(&decoded, RECEIVE).unwrap()
        else {
            panic!("expected HTX quote")
        };
        assert_eq!(
            quotes,
            vec![SpotQuote {
                symbol: "BTCUSDT".into(),
                bid: "5.266501e4".into(),
                ask: "52665.020000000000000001".into(),
                source_ms: RECEIVE - 1,
                receive_ms: RECEIVE,
                sequence: Some(137_005_210_233),
            }]
        );
        let wrong = std::str::from_utf8(quote)
            .unwrap()
            .replace(".bbo", ".detail");
        assert!(matches!(
            protocol.decode(wrong.as_bytes(), RECEIVE),
            Ok(Packet::Reception(SpotReception::Idle))
        ));
        let wrong = std::str::from_utf8(quote)
            .unwrap()
            .replace(r#""symbol":"btcusdt""#, r#""symbol":"ethusdt""#);
        assert!(matches!(
            protocol.decode(wrong.as_bytes(), RECEIVE),
            Ok(Packet::Reception(SpotReception::Idle))
        ));
        let version = std::str::from_utf8(quote)
            .unwrap()
            .replace(r#""seqId":137005210233"#, r#""seqId":0,"version":42"#);
        let Packet::Reception(SpotReception::Quotes(quotes)) =
            protocol.decode(version.as_bytes(), RECEIVE).unwrap()
        else {
            panic!("expected HTX version fallback")
        };
        assert_eq!(quotes[0].sequence, Some(42));
        assert!(protocol
            .decode(
                br#"{"ch":"ignored","extra":[{"symbol":1,"sym\u0062ol":2}]}"#,
                RECEIVE
            )
            .is_err());
    }

    #[test]
    fn htx_gzip_limits_crc_and_member_boundaries() {
        assert!(inflate(&vec![0; MAX_LINE + 1]).is_err());
        assert_eq!(inflate(&gzip(&vec![0; MAX_LINE])).unwrap().len(), MAX_LINE);
        assert!(inflate(&gzip(&vec![0; MAX_LINE + 1])).is_err());
        let encoded = gzip(b"{}");
        let mut invalid = encoded.clone();
        invalid[encoded.len() - 8] ^= 1;
        assert!(inflate(&invalid).is_err());
        assert!(inflate(&encoded[..encoded.len() - 1]).is_err());
        assert!(inflate(&[encoded.clone(), encoded].concat()).is_err());
    }

    #[test]
    fn htx_public_endpoint_policy_rejects_private_paths() {
        let remote = "192.0.2.1:443".parse().unwrap();
        assert!(request(remote, "wss://api.huobi.pro/ws").is_ok());
        assert!(request(remote, "wss://api-aws.huobi.pro/ws").is_ok());
        assert!(request(remote, "wss://api.huobi.pro/ws/v2").is_err());
        assert!(request(remote, "wss://api.huobi.pro/ws?x=1").is_err());
        assert!(request(remote, "wss://ws.okx.com/ws/v5/public").is_err());
    }

    #[test]
    fn htx_application_heartbeat_starts_at_ack_and_expires_after_three_intervals() {
        let mut heartbeat = Heartbeat::default();
        let ack = Instant::now() + Duration::from_secs(30);
        heartbeat.ping(ack - Duration::from_secs(10));
        assert!(heartbeat.check(ack).is_ok());
        heartbeat.acknowledged(ack);
        let just_before_expiry = HEARTBEAT_TIMEOUT - Duration::from_millis(1);
        assert!(heartbeat.check(ack + just_before_expiry).is_ok());
        assert_eq!(
            heartbeat.check(ack + HEARTBEAT_TIMEOUT).unwrap_err(),
            "HTX application heartbeat timeout"
        );

        let ping = ack + Duration::from_secs(5);
        heartbeat.ping(ping);
        assert!(heartbeat.check(ack + HEARTBEAT_TIMEOUT).is_ok());
        assert!(heartbeat.check(ping + just_before_expiry).is_ok());
        assert!(heartbeat.check(ping + HEARTBEAT_TIMEOUT).is_err());
    }

    #[test]
    fn htx_sequence_uses_exact_legacy_integer_or_fallback() {
        let mut protocol = fixture();
        let ack = br#"{"status":"ok","subbed":"market.btcusdt.bbo","id":"bbo1"}"#;
        assert!(matches!(
            protocol.decode(ack, 1),
            Ok(Packet::Reception(SpotReception::Acknowledged))
        ));
        for (fields, expected) in [
            ("", None),
            (r#""seqId":null,"#, None),
            (r#""seqId":0,"#, None),
            (r#""seqId":0,"version":null,"#, None),
            (r#""version":0,"#, Some(0)),
            (r#""seqId":null,"version":0,"#, Some(0)),
            (r#""seqId":0,"version":0,"#, Some(0)),
            (r#""seqId":0,"version":27,"#, Some(27)),
            (r#""seqId":44,"version":27,"#, Some(44)),
        ] {
            let quote = format!(
                r#"{{"ch":"market.btcusdt.bbo","tick":{{{fields}"symbol":"btcusdt","bid":1,"ask":2,"quoteTime":1}}}}"#
            );
            let Packet::Reception(SpotReception::Quotes(quotes)) =
                protocol.decode(quote.as_bytes(), 1).unwrap()
            else {
                panic!("expected HTX quote for {fields:?}");
            };
            assert_eq!(quotes[0].sequence, expected, "{fields:?}");
        }
    }

    #[test]
    fn htx_target_requires_exact_lowercase_equivalent() {
        let binding = |wire: &str| BTreeMap::from([("BTCUSDC".into(), wire.into())]);
        assert!(validate_instruments(&binding("btcusdc")).is_ok());
        assert!(validate_instruments(&binding("ethusdc")).is_err());
        assert!(validate_instruments(&binding("BTCUSDC")).is_err());
        assert!(validate_instruments(&binding("btc-usdc")).is_err());
    }
}
