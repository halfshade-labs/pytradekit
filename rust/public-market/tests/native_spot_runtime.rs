use flate2::{write::GzEncoder, Compression};
use ptk_public_market::runtime::{
    handoff::{Phase, Targets},
    Endpoint, Runtime, Settings, SpotSubscription,
};
use ptk_public_market::transport::SpotVenue;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::Write,
    net::{TcpListener, TcpStream},
    thread,
    time::{Duration, Instant},
};
use tungstenite::{Message, WebSocket};

const WAIT: Duration = Duration::from_secs(4);
const SLICE: Duration = Duration::from_millis(10);
const WALL: i64 = 1_800_000_000_000;

struct Peer(TcpListener);

impl Peer {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        Self(listener)
    }

    fn endpoint(&self) -> Endpoint {
        let address = self.0.local_addr().unwrap();
        Endpoint {
            address,
            uri: format!("ws://localhost:{}/public-fixture", address.port()),
        }
    }

    fn accept(&self) -> WebSocket<TcpStream> {
        let deadline = Instant::now() + WAIT;
        loop {
            assert!(Instant::now() < deadline, "physical worker did not connect");
            if let Ok((stream, _)) = self.0.accept() {
                stream.set_read_timeout(Some(WAIT)).unwrap();
                stream.set_write_timeout(Some(WAIT)).unwrap();
                return tungstenite::accept(stream).unwrap();
            }
            thread::sleep(SLICE);
        }
    }
}

fn read_json(peer: &mut WebSocket<TcpStream>) -> Value {
    let data = peer.read().unwrap().into_text().unwrap();
    serde_json::from_str(&data).unwrap()
}

fn text(peer: &mut WebSocket<TcpStream>, value: Value) {
    peer.send(Message::Text(value.to_string().into())).unwrap()
}

fn gzip(peer: &mut WebSocket<TcpStream>, value: Value) {
    let mut frame = GzEncoder::new(Vec::new(), Compression::fast());
    frame.write_all(value.to_string().as_bytes()).unwrap();
    peer.send(Message::Binary(frame.finish().unwrap().into()))
        .unwrap();
}

fn next(runtime: &mut Runtime, predicate: impl Fn(&Value) -> bool) -> Value {
    let deadline = Instant::now() + WAIT;
    loop {
        assert!(Instant::now() < deadline, "missing runtime row");
        for row in runtime.recv(SLICE).unwrap() {
            if predicate(&row) {
                return row;
            }
        }
    }
}

fn physical_connected(runtime: &Runtime, venue: &str) -> bool {
    runtime
        .control()
        .physical
        .lock()
        .unwrap()
        .values()
        .any(|row| row["venue"] == venue && row["connected"] == true)
}

fn htx_quote(channel: &str, instrument: &str, sequence: u64) -> Value {
    json!({"ch":channel,"tick":{"symbol":instrument,"quoteTime":WALL,"seqId":sequence,"bid":1.01,"ask":1.02}})
}

fn okx_quote(count: usize) -> Value {
    let rows: Vec<_> = (0..count).map(|index| json!({
        "instType":"SPOT","instId":"BTC-USDT","bidPx":"1.01","askPx":"1.02","ts":(WALL + index as i64).to_string()
    })).collect();
    json!({"arg":{"channel":"tickers","instId":"BTC-USDT"},"data":rows})
}

fn htx_ack(peer: &mut WebSocket<TcpStream>, request: &Value) {
    gzip(
        peer,
        json!({"status":"ok","subbed":request["sub"],"id":request["id"]}),
    );
}

fn is_connected(row: &Value, venue: &str) -> bool {
    row["type"] == "native_spot_route" && row["venue"] == venue && row["connected"] == true
}

#[test]
fn live_native_spot_workers_isolate_ack_disconnect_retarget_and_stop() {
    let perp = Peer::new();
    let htx = Peer::new();
    let okx = Peer::new();
    let htx_btc = BTreeMap::from([("BTCUSDT".into(), "btcusdt".into())]);
    let okx_btc = BTreeMap::from([("BTCUSDT".into(), "BTC-USDT".into())]);
    let settings = Settings {
        session: "multi".into(),
        mode: "single".into(),
        perpetual: perp.endpoint(),
        spot: perp.endpoint(),
        perpetual_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        spot_symbols: vec![],
        fixture_wall_ms: Some(WALL),
        fixture_backup_endpoint: None,
    };
    let mut runtime = Runtime::start_configured_with_native_spot(
        settings,
        BTreeMap::from([
            (
                SpotVenue::Htx,
                SpotSubscription {
                    endpoint: htx.endpoint(),
                    instruments: htx_btc,
                },
            ),
            (
                SpotVenue::Okx,
                SpotSubscription {
                    endpoint: okx.endpoint(),
                    instruments: okx_btc.clone(),
                },
            ),
        ]),
    )
    .unwrap();
    let mut perp_peer = perp.accept();
    let mut htx_peer = htx.accept();
    let mut okx_peer = okx.accept();
    let perp_request = read_json(&mut perp_peer);
    let htx_request = read_json(&mut htx_peer);
    let okx_request = read_json(&mut okx_peer);
    assert_eq!(htx_request["sub"], "market.btcusdt.bbo");
    assert_eq!(
        okx_request,
        json!({"op":"subscribe","args":[{"channel":"tickers","instId":"BTC-USDT"}]})
    );
    text(
        &mut perp_peer,
        json!({"id":perp_request["id"],"result":null}),
    );
    text(
        &mut okx_peer,
        json!({"event":"subscribe","arg":{"channel":"tickers","instId":"BTC-USDT"}}),
    );
    let okx_initial = next(&mut runtime, |row| is_connected(row, "OKX"));
    gzip(&mut htx_peer, htx_quote("market.btcusdt.bbo", "btcusdt", 1));
    gzip(&mut htx_peer, json!({"ping":9_007_199_254_740_993_i64}));
    assert_eq!(
        read_json(&mut htx_peer),
        json!({"pong":9_007_199_254_740_993_i64})
    );
    let batch_sent_after = runtime.clock().wall_ms;
    text(&mut okx_peer, okx_quote(2));
    let mut first_capture = None;
    for offset in 0..2 {
        let row = next(&mut runtime, |row| {
            assert_ne!(row["venue"], "HTX");
            row["type"] == "native_spot_quote"
        });
        assert_eq!(row["generation"], okx_initial["generation"]);
        assert_eq!(row["quote"]["source_ms"], WALL + offset);
        assert_eq!(row["quote"]["sequence"], Value::Null);
        assert_eq!(row["batch_has_more"], offset == 0);
        let capture = row["quote"]["receive_ms"].as_i64().unwrap();
        let processed = row["now_ms"].as_i64().unwrap();
        assert!((batch_sent_after..=processed).contains(&capture));
        if let Some((first, first_processed)) = first_capture {
            assert_eq!(capture, first);
            assert!(processed > first_processed);
        } else {
            first_capture = Some((capture, processed));
            thread::sleep(SLICE);
        }
    }

    htx_ack(&mut htx_peer, &htx_request);
    let htx_initial = next(&mut runtime, |row| is_connected(row, "HTX"));
    gzip(
        &mut htx_peer,
        htx_quote("market.btcusdt.detail", "btcusdt", 2),
    );
    gzip(&mut htx_peer, htx_quote("market.btcusdt.bbo", "btcusdt", 3));
    let quote = next(&mut runtime, |row| row["type"] == "native_spot_quote");
    assert_eq!(quote["quote"]["sequence"], 3);
    assert_eq!(quote["batch_has_more"], false);

    // Queue an old callback, then withdraw physical truth before dequeuing.
    let count = runtime.accounting()["wire_raw_received"].as_u64().unwrap();
    gzip(&mut htx_peer, htx_quote("market.btcusdt.bbo", "btcusdt", 4));
    htx_peer.close(None).unwrap();
    let until = Instant::now() + WAIT;
    while physical_connected(&runtime, "HTX") {
        assert!(Instant::now() < until);
        thread::sleep(SLICE);
    }
    assert_eq!(runtime.accounting()["wire_raw_received"], count + 1);
    let disconnected = next(&mut runtime, |row| {
        assert!(row["type"] != "native_spot_quote" || row["venue"] != "HTX");
        row["type"] == "native_spot_route" && row["venue"] == "HTX" && row["connected"] == false
    });
    assert_eq!(disconnected["generation"], htx_initial["generation"]);
    assert_eq!(runtime.accounting()["retired_frames"], 1);
    text(&mut okx_peer, okx_quote(1));
    assert_eq!(
        next(&mut runtime, |row| row["type"] == "native_spot_quote")["generation"],
        okx_initial["generation"]
    );

    // Reconnect HTX and subsequently replace its targets while OKX is offline.
    let mut htx_peer = htx.accept();
    let request = read_json(&mut htx_peer);
    htx_ack(&mut htx_peer, &request);
    let reconnected = next(&mut runtime, |row| is_connected(row, "HTX"));
    assert!(
        reconnected["generation"].as_u64().unwrap() > htx_initial["generation"].as_u64().unwrap()
    );
    okx_peer.close(None).unwrap();
    drop(okx);
    next(&mut runtime, |row| {
        row["type"] == "native_spot_route" && row["venue"] == "OKX" && row["connected"] == false
    });

    let targets = Targets {
        perpetual: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        spot: vec![],
        native_spot: BTreeMap::from([
            (
                SpotVenue::Htx,
                BTreeMap::from([("ETHUSDT".into(), "ethusdt".into())]),
            ),
            (SpotVenue::Okx, okx_btc),
        ]),
    };
    runtime.prepare_handoff(1, targets).unwrap();
    let control = runtime.control();
    let slot = control.handoff.as_ref().unwrap();
    slot.lock().unwrap().commit_logical(1, || Ok(())).unwrap();
    let retired = runtime.progress_handoff().unwrap();
    assert_eq!(retired.len(), 1);
    assert_eq!(retired[0]["venue"], "HTX");
    let deadline = Instant::now() + WAIT;
    while runtime.handoff_status().unwrap()["active_workers"] != 3 {
        assert!(Instant::now() < deadline);
        runtime.progress_handoff().unwrap();
        thread::sleep(SLICE);
    }
    assert!(matches!(htx_peer.read().unwrap(), Message::Close(_)));
    let mut htx_peer = htx.accept();
    let request = read_json(&mut htx_peer);
    assert_eq!(request["sub"], "market.ethusdt.bbo");
    htx_ack(&mut htx_peer, &request);
    let replaced = next(&mut runtime, |row| is_connected(row, "HTX"));
    assert!(replaced["generation"].as_u64().unwrap() > reconnected["generation"].as_u64().unwrap());
    runtime.progress_handoff().unwrap();
    assert_eq!(slot.lock().unwrap().status().0.unwrap().phase, Phase::Ready);
    {
        let truth = control.physical.lock().unwrap();
        let mut slot = slot.lock().unwrap();
        slot.activate(1, &truth).unwrap();
        slot.finish_active().unwrap();
    }
    gzip(&mut htx_peer, htx_quote("market.ethusdt.bbo", "ethusdt", 1));
    assert_eq!(
        next(&mut runtime, |row| row["type"] == "native_spot_quote")["quote"]["symbol"],
        "ETHUSDT"
    );
    runtime.request_handoff_shutdown().unwrap();
    let deadline = Instant::now() + WAIT;
    while !runtime.handoff_shutdown_ready().unwrap() {
        assert!(Instant::now() < deadline, "worker cleanup timed out");
        thread::sleep(SLICE);
    }
    runtime.shutdown().unwrap();
    assert_eq!(runtime.accounting()["workers"], 0);
    assert_eq!(runtime.accounting()["first_failure"], Value::Null);
}

fn venue_acks(peer: &mut WebSocket<TcpStream>, venue: SpotVenue) -> BTreeMap<String, Value> {
    match venue {
        SpotVenue::Htx => (0..3)
            .map(|_| {
                let request = read_json(peer);
                let channel = request["sub"].as_str().unwrap();
                (
                    channel.to_owned(),
                    json!({"status":"ok","subbed":channel,"id":request["id"]}),
                )
            })
            .collect(),
        SpotVenue::Okx => {
            let request = read_json(peer);
            assert_eq!(request["op"], "subscribe");
            request["args"]
                .as_array()
                .unwrap()
                .iter()
                .map(|arg| {
                    (
                        arg["instId"].as_str().unwrap().into(),
                        json!({"event":"subscribe","arg":arg}),
                    )
                })
                .collect()
        }
    }
}

fn send_venue(peer: &mut WebSocket<TcpStream>, venue: SpotVenue, value: Value) {
    match venue {
        SpotVenue::Htx => gzip(peer, value),
        SpotVenue::Okx => text(peer, value),
    }
}

fn venue_quote(venue: SpotVenue, base: &str, sequence: u64) -> Value {
    match venue {
        SpotVenue::Htx => {
            let symbol = format!("{}usdt", base.to_ascii_lowercase());
            htx_quote(&format!("market.{symbol}.bbo"), &symbol, sequence)
        }
        SpotVenue::Okx => {
            let instrument = format!("{base}-USDT");
            json!({"arg":{"channel":"tickers","instId":instrument},"data":[
                {"instId":instrument,"bidPx":"1","askPx":"2","ts":WALL.to_string(),"seqId":sequence}
            ]})
        }
    }
}

fn peer_barrier(peer: &mut WebSocket<TcpStream>) {
    let marker = vec![1, 2, 3];
    peer.send(Message::Ping(marker.clone().into())).unwrap();
    assert_eq!(peer.read().unwrap(), Message::Pong(marker.into()));
}

#[test]
fn actual_symbol_ack_retains_latest_capture_until_venue_ack() {
    for venue in [SpotVenue::Htx, SpotVenue::Okx] {
        let perp = Peer::new();
        let spot = Peer::new();
        let symbols = ["BTC", "ETH", "SOL"];
        let route_key = |base: &str| match venue {
            SpotVenue::Htx => format!("market.{}usdt.bbo", base.to_ascii_lowercase()),
            SpotVenue::Okx => format!("{base}-USDT"),
        };
        let instruments = symbols
            .into_iter()
            .map(|base| {
                let key = match venue {
                    SpotVenue::Htx => format!("{}usdt", base.to_ascii_lowercase()),
                    SpotVenue::Okx => route_key(base),
                };
                (format!("{base}USDT"), key)
            })
            .collect();
        let settings = Settings {
            session: "partial-ack".into(),
            mode: "single".into(),
            perpetual: perp.endpoint(),
            spot: perp.endpoint(),
            perpetual_symbols: symbols.map(|base| format!("{base}USDT")).to_vec(),
            spot_symbols: vec![],
            fixture_wall_ms: Some(WALL),
            fixture_backup_endpoint: None,
        };
        let mut runtime = Runtime::start_configured_with_native_spot(
            settings,
            BTreeMap::from([(
                venue,
                SpotSubscription {
                    endpoint: spot.endpoint(),
                    instruments,
                },
            )]),
        )
        .unwrap();
        let mut perp_peer = perp.accept();
        let perp_request = read_json(&mut perp_peer);
        text(
            &mut perp_peer,
            json!({"id":perp_request["id"],"result":null}),
        );

        let mut failed = spot.accept();
        let acks = venue_acks(&mut failed, venue);
        send_venue(&mut failed, venue, acks[&route_key("BTC")].clone());
        send_venue(&mut failed, venue, venue_quote(venue, "BTC", 99));
        peer_barrier(&mut failed);
        assert_eq!(runtime.accounting()["wire_raw_received"], 0);
        send_venue(
            &mut failed,
            venue,
            match venue {
                SpotVenue::Htx => json!({"status":"error"}),
                SpotVenue::Okx => json!({"event":"error"}),
            },
        );

        let mut peer = spot.accept();
        let acks = venue_acks(&mut peer, venue);
        assert_eq!(acks.len(), 3);
        send_venue(&mut peer, venue, acks[&route_key("BTC")].clone());
        send_venue(&mut peer, venue, venue_quote(venue, "BTC", 1));
        send_venue(&mut peer, venue, acks[&route_key("ETH")].clone());
        send_venue(&mut peer, venue, venue_quote(venue, "ETH", 2));
        peer_barrier(&mut peer);
        let eth_before = runtime.clock().wall_ms;
        thread::sleep(SLICE);
        send_venue(&mut peer, venue, venue_quote(venue, "BTC", 3));
        send_venue(&mut peer, venue, venue_quote(venue, "BTC", 2));
        send_venue(&mut peer, venue, venue_quote(venue, "SOL", 4));
        peer_barrier(&mut peer);
        let btc_before = runtime.clock().wall_ms;
        assert!(runtime
            .recv(Duration::ZERO)
            .unwrap()
            .iter()
            .all(|row| row["venue"] != venue.name()));
        assert!(!physical_connected(&runtime, venue.name()));
        assert_eq!(runtime.accounting()["wire_raw_received"], 0);

        thread::sleep(SLICE);
        let ack_after = runtime.clock().wall_ms;
        send_venue(&mut peer, venue, acks[&route_key("SOL")].clone());
        let route = next(&mut runtime, |row| {
            if row["venue"] != venue.name() {
                return false;
            }
            assert!(
                is_connected(row, venue.name()),
                "quote delivered before the aggregate ACK"
            );
            true
        });
        for (symbol, sequence, capture_before) in
            [("ETHUSDT", 2, eth_before), ("BTCUSDT", 3, btc_before)]
        {
            let row = next(&mut runtime, |row| row["type"] == "native_spot_quote");
            assert_eq!(row["generation"], route["generation"]);
            assert_eq!(row["quote"]["symbol"], symbol);
            assert_eq!(row["quote"]["sequence"], sequence);
            assert_eq!(row["quote"]["source_ms"], WALL);
            assert_eq!(row["batch_has_more"], false);
            let capture = row["quote"]["receive_ms"].as_i64().unwrap();
            assert!(capture <= capture_before);
            if symbol == "BTCUSDT" {
                assert!(capture > eth_before);
            }
            assert!(row["now_ms"].as_i64().unwrap() >= ack_after);
        }
        assert_eq!(runtime.accounting()["wire_raw_received"], 2);
        assert_eq!(runtime.accounting()["supplied"], 2);
        assert_eq!(runtime.accounting()["retired_frames"], 0);
        assert!(
            runtime.accounting()["queue_age"]["max_ms"]
                .as_i64()
                .unwrap()
                >= ack_after - eth_before
        );
        runtime.request_handoff_shutdown().unwrap();
        let deadline = Instant::now() + WAIT;
        while !runtime.handoff_shutdown_ready().unwrap() {
            assert!(Instant::now() < deadline);
            thread::sleep(SLICE);
        }
        runtime.shutdown().unwrap();
        assert_eq!(runtime.accounting()["first_failure"], Value::Null);
    }
}
