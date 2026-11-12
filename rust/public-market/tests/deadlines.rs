use ptk_public_market::transport::{PublicSocket, Reception};
use std::io::{Read, Write};
use std::net::{Shutdown, SocketAddr, TcpListener, TcpStream};
use std::sync::{atomic::{AtomicBool, Ordering}, mpsc, Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};
use tungstenite::Message;

const ACCEPT_LIMIT: Duration = Duration::from_secs(3);
const FRAME_HORIZON: Duration = Duration::from_secs(2);
const FRAME_INTERVAL: Duration = Duration::from_millis(20);
const RECEIVE_LIMIT: Duration = Duration::from_millis(750);
const HANDSHAKE_HORIZON: Duration = Duration::from_secs(8);
const HEADER_INTERVAL: Duration = Duration::from_millis(100);
const CONNECT_LIMIT: Duration = Duration::from_secs(6);

#[derive(Clone, Copy)]
enum Scenario { Fragments, SlowHandshake }

#[derive(Default)]
struct Progress { writes: usize, first: Option<Instant>, last: Option<Instant>, max_gap: Duration }
impl Progress {
    fn record(&mut self) {
        let now = Instant::now();
        if let Some(last) = self.last { self.max_gap = self.max_gap.max(now.duration_since(last)); }
        self.first.get_or_insert(now);
        self.last = Some(now);
        self.writes += 1;
    }
    fn span(&self) -> Duration {
        self.first.zip(self.last).map_or(Duration::ZERO, |(first, last)| last.duration_since(first))
    }
}

struct Fixture {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    active: Arc<Mutex<Option<TcpStream>>>,
    started: mpsc::Receiver<()>,
    join: Option<JoinHandle<Result<Progress, String>>>,
}
impl Fixture {
    fn start(scenario: Scenario) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let active = Arc::new(Mutex::new(None));
        let (started, observed) = mpsc::sync_channel(1);
        let (server_stop, server_active) = (stop.clone(), active.clone());
        let join = thread::spawn(move || {
            let stream = accept_before(&listener, &server_stop)?;
            stream.set_nodelay(true).map_err(|error| error.to_string())?;
            stream.set_read_timeout(Some(Duration::from_millis(250))).map_err(|error| error.to_string())?;
            stream.set_write_timeout(Some(Duration::from_millis(250))).map_err(|error| error.to_string())?;
            *server_active.lock().unwrap() = Some(stream.try_clone().map_err(|error| error.to_string())?);
            match scenario {
                Scenario::Fragments => send_fragments(stream, &server_stop, started),
                Scenario::SlowHandshake => send_partial_handshake(stream, &server_stop),
            }
        });
        Self { address, stop, active, started: observed, join: Some(join) }
    }
    fn uri(&self) -> String { format!("ws://localhost:{}/deadline", self.address.port()) }
    fn finish(&mut self) -> Result<Progress, String> {
        self.stop.store(true, Ordering::Release);
        if let Some(stream) = self.active.lock().unwrap_or_else(|error| error.into_inner()).as_ref() {
            let _ = stream.shutdown(Shutdown::Both);
        }
        self.join.take().ok_or("fixture already joined")?.join().map_err(|_| "deadline fixture panicked".to_string())?
    }
}
impl Drop for Fixture {
    fn drop(&mut self) { if self.join.is_some() { let _ = self.finish(); } }
}

fn accept_before(listener: &TcpListener, stop: &AtomicBool) -> Result<TcpStream, String> {
    let until = Instant::now() + ACCEPT_LIMIT;
    while !stop.load(Ordering::Acquire) && Instant::now() < until {
        match listener.accept() {
            Ok((stream, _)) => return Ok(stream),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(2)),
            Err(error) => return Err(error.to_string()),
        }
    }
    Err("deadline fixture accept limit".into())
}

fn send_fragments(stream: TcpStream, stop: &AtomicBool, started: mpsc::SyncSender<()>) -> Result<Progress, String> {
    let mut socket = tungstenite::accept(stream).map_err(|error| error.to_string())?;
    let request = socket.read().map_err(|error| error.to_string())?.into_text().map_err(|error| error.to_string())?;
    let request: serde_json::Value = serde_json::from_str(&request).map_err(|error| error.to_string())?;
    if request["method"] != "SUBSCRIBE" || request["id"] != 1 || request["params"] != serde_json::json!(["btcusdt@bookTicker"]) {
        return Err("deadline fixture expected actual SUBSCRIBE".into());
    }
    socket.send(Message::Text("{\"id\":1,\"result\":null}".into())).map_err(|error| error.to_string())?;
    // RFC 6455 server wire frames: FIN=0/Text with one UTF-8 byte, then
    // FIN=0/Continuation with zero payload. Message size never grows to MAX_LINE.
    socket.get_mut().write_all(&[0x01, 0x01, b'{']).map_err(|error| error.to_string())?;
    let mut progress = Progress::default();
    progress.record();
    let _ = started.send(());
    let until = Instant::now() + FRAME_HORIZON;
    while !stop.load(Ordering::Acquire) && Instant::now() < until {
        thread::sleep(FRAME_INTERVAL);
        if stop.load(Ordering::Acquire) { break; }
        if socket.get_mut().write_all(&[0x00, 0x00]).is_err() { break; }
        progress.record();
    }
    // No FIN is ever sent. Finite TCP close bounds the unfixed read even when
    // each successful continuation restarts its unchanged 250ms syscall limit.
    let _ = socket.get_mut().shutdown(Shutdown::Both);
    Ok(progress)
}

fn read_upgrade(stream: &mut TcpStream) -> Result<String, String> {
    let until = Instant::now() + ACCEPT_LIMIT;
    let mut request = Vec::new();
    while request.len() < 8192 && Instant::now() < until {
        let mut byte = [0];
        stream.read_exact(&mut byte).map_err(|error| error.to_string())?;
        request.push(byte[0]);
        if request.ends_with(b"\r\n\r\n") { return String::from_utf8(request).map_err(|error| error.to_string()); }
    }
    Err("deadline fixture incomplete client upgrade".into())
}

fn send_partial_handshake(mut stream: TcpStream, stop: &AtomicBool) -> Result<Progress, String> {
    let request = read_upgrade(&mut stream)?;
    let key = request.lines().filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.eq_ignore_ascii_case("sec-websocket-key"))
        .map(|(_, value)| value.trim()).ok_or("deadline fixture missing upgrade key")?;
    let accept = tungstenite::handshake::derive_accept_key(key.as_bytes());
    let header = format!("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\nX-Deadline-Progress: ");
    stream.write_all(header.as_bytes()).map_err(|error| error.to_string())?;
    let mut progress = Progress::default();
    progress.record();
    let until = Instant::now() + HANDSHAKE_HORIZON;
    // Fewer than 100 scheduled writes, under 24 KiB, with >=256 bytes/chunk:
    // tungstenite 0.28 AttackCheck's packet/byte thresholds do not end this case.
    while !stop.load(Ordering::Acquire) && Instant::now() < until {
        if stream.write_all(&[b'p'; 256]).is_err() { break; }
        progress.record();
        thread::sleep(HEADER_INTERVAL);
    }
    // The HTTP response stays partial; never complete its header terminator.
    let _ = stream.shutdown(Shutdown::Both);
    Ok(progress)
}

#[test]
fn foundation_io_empty_continuations_preserve_receive_deadline() {
    let mut fixture = Fixture::start(Scenario::Fragments);
    let mut client = PublicSocket::connect(fixture.address, &fixture.uri()).unwrap();
    client.subscribe(1, &["BTCUSDT".into()]).unwrap();
    assert_eq!(client.receive().unwrap()["type"], "ack");
    fixture.started.recv_timeout(ACCEPT_LIMIT).unwrap();
    let start = Instant::now();
    let reception = client.receive_timeout();
    let elapsed = start.elapsed();
    drop(client);
    let progress = fixture.finish().unwrap();
    assert!(progress.writes >= 2 && progress.max_gap < Duration::from_millis(200), "fragment fixture failed to sustain sub-250ms progress: writes={}, max_gap={:?}", progress.writes, progress.max_gap);
    assert!(matches!(&reception, Ok(Reception::Idle)) && elapsed <= RECEIVE_LIMIT,
        "actual receive_timeout must yield Idle within {RECEIVE_LIMIT:?}; elapsed={elapsed:?}, idle={}, error={:?}, sender_horizon={FRAME_HORIZON:?}",
        matches!(&reception, Ok(Reception::Idle)), reception.as_ref().err());
}

#[test]
fn foundation_io_progressing_http_upgrade_preserves_connect_deadline() {
    let mut fixture = Fixture::start(Scenario::SlowHandshake);
    let start = Instant::now();
    let connection = PublicSocket::connect(fixture.address, &fixture.uri());
    let elapsed = start.elapsed();
    let failed = connection.is_err();
    let error = connection.as_ref().err().cloned();
    drop(connection);
    let progress = fixture.finish().unwrap();
    assert!(progress.span() >= Duration::from_millis(4500) && progress.max_gap < Duration::from_millis(200),
        "HTTP fixture must establish sustained progress before the five-second budget: writes={}, span={:?}, max_gap={:?}, error={error:?}", progress.writes, progress.span(), progress.max_gap);
    assert!(failed && elapsed <= CONNECT_LIMIT,
        "actual connect must fail within {CONNECT_LIMIT:?} under partial HTTP progress; elapsed={elapsed:?}, error={error:?}, sender_horizon={HANDSHAKE_HORIZON:?}");
}
