//! Socket/TLS/WebSocket reception owned by the caller's route worker.
//! Each connected socket has read/write deadlines and bounded frame buffers.
use crate::{Result, MAX_LINE};
use serde_json::{json, Value};
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};
use tungstenite::client::IntoClientRequest;
use tungstenite::{client_tls_with_config, stream::MaybeTlsStream, Message, WebSocket};
use tungstenite::protocol::WebSocketConfig;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);
const IO_TIMEOUT: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, Copy)]
enum IoPhase { Establishing, Established }

/// The owner resets one absolute deadline per operation, below TLS and framing.
#[derive(Debug)]
struct DeadlineStream {
    stream: TcpStream,
    deadline: Instant,
    phase: IoPhase,
}
impl DeadlineStream {
    fn reset_deadline(&mut self) -> io::Result<()> {
        self.deadline = checked_deadline(IO_TIMEOUT)?;
        self.phase = IoPhase::Established;
        Ok(())
    }
    fn remaining_timeout(&self) -> io::Result<Duration> {
        remaining_time(self.deadline).map(|time| time.min(IO_TIMEOUT))
            .map_err(|error| normalize_timeout(error, self.phase))
    }
}
impl Read for DeadlineStream {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        self.stream.set_read_timeout(Some(self.remaining_timeout()?))?;
        self.stream.read(buffer).map_err(|error| normalize_timeout(error, self.phase))
    }
}
impl Write for DeadlineStream {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        self.stream.set_write_timeout(Some(self.remaining_timeout()?))?;
        self.stream.write(buffer).map_err(|error| normalize_timeout(error, self.phase))
    }
    fn flush(&mut self) -> io::Result<()> {
        self.stream.set_write_timeout(Some(self.remaining_timeout()?))?;
        self.stream.flush().map_err(|error| normalize_timeout(error, self.phase))
    }
}
fn checked_deadline(timeout: Duration) -> io::Result<Instant> {
    Instant::now().checked_add(timeout)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "physical socket deadline overflow"))
}
fn remaining_time(deadline: Instant) -> io::Result<Duration> {
    deadline.checked_duration_since(Instant::now()).filter(|time| !time.is_zero())
        .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "physical socket operation deadline"))
}
fn normalize_timeout(error: io::Error, phase: IoPhase) -> io::Error {
    // Blocking establishment cannot use tungstenite's nonblocking TLS handshake.
    // Established TLS BIOs need WouldBlock to retain retryable framing/TLS state.
    match (phase, error.kind()) {
        (IoPhase::Establishing, io::ErrorKind::WouldBlock) => io::Error::new(io::ErrorKind::TimedOut, error),
        (IoPhase::Established, io::ErrorKind::TimedOut) => io::Error::new(io::ErrorKind::WouldBlock, error),
        _ => error,
    }
}
fn is_timeout(error: &tungstenite::Error) -> bool {
    matches!(error, tungstenite::Error::Io(error)
        if matches!(error.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut))
}

pub enum Reception { Event(Value), Idle }

pub struct PublicSocket {
    socket: WebSocket<MaybeTlsStream<DeadlineStream>>,
    pending: Option<u64>,
    pub messages: u64,
    pub bytes: u64,
}
impl PublicSocket {
    /// Address resolution and allowed endpoint policy belong to explicit control.
    /// A supplied loopback address permits real offline framing/TLS fixtures.
    pub fn connect(address: SocketAddr, uri: &str) -> Result<Self> {
        Self::connect_with(address,uri,None)
    }
    /// Offline TLS fixtures can trust their ephemeral CA only on loopback.
    /// Certificate and hostname validation remain enabled.
    pub fn connect_loopback_with_ca(address: SocketAddr,uri: &str,ca_pem: &[u8])->Result<Self> {
        if !address.ip().is_loopback() {return Err("fixture CA requires loopback address".into())}
        let certificate=native_tls::Certificate::from_pem(ca_pem).map_err(|e|e.to_string())?;
        let tls=native_tls::TlsConnector::builder().add_root_certificate(certificate).build().map_err(|e|e.to_string())?;
        Self::connect_with(address,uri,Some(tungstenite::Connector::NativeTls(tls)))
    }
    pub fn validate_endpoint(address: SocketAddr, uri: &str) -> Result<()> {
        let request = uri
            .into_client_request()
            .map_err(|_| "invalid public websocket endpoint URI")?;
        let host = request.uri().host().ok_or("missing public endpoint host")?;
        let approved_public = request.uri().scheme_str() == Some("wss")
            && matches!(host, "fstream.binance.com" | "stream.binance.com");
        if !approved_public && !address.ip().is_loopback() {
            return Err("unapproved public endpoint".into());
        }
        if !matches!(request.uri().scheme_str(), Some("ws" | "wss")) {
            return Err("unsupported public websocket endpoint scheme".into());
        }
        Ok(())
    }
    fn connect_with(address:SocketAddr,uri:&str,connector:Option<tungstenite::Connector>)->Result<Self> {
        Self::validate_endpoint(address, uri)?;
        let request=uri.into_client_request().map_err(|e|e.to_string())?;
        let deadline=checked_deadline(CONNECT_TIMEOUT).map_err(|e|e.to_string())?;
        let timeout=remaining_time(deadline).map_err(|e|e.to_string())?;
        let stream=TcpStream::connect_timeout(&address,timeout).map_err(|e|e.to_string())?;
        let stream=DeadlineStream{stream,deadline,phase:IoPhase::Establishing};
        let config=WebSocketConfig::default().max_message_size(Some(MAX_LINE)).max_frame_size(Some(MAX_LINE)).write_buffer_size(16384).max_write_buffer_size(MAX_LINE*2);
        let (socket,_)=client_tls_with_config(request,stream,Some(config),connector).map_err(|e|e.to_string())?;
        remaining_time(deadline).map_err(|e|e.to_string())?;
        Ok(Self{socket,pending:None,messages:0,bytes:0})
    }
    fn begin_operation(&mut self) -> Result<()> {
        let stream=match self.socket.get_mut() {
            MaybeTlsStream::Plain(stream)=>stream,
            MaybeTlsStream::NativeTls(stream)=>stream.get_mut(),
            _=>return Err("unsupported physical socket stream".into()),
        };
        stream.reset_deadline().map_err(|e|e.to_string())
    }
    pub fn subscribe(&mut self,request: u64,symbols: &[String]) -> Result<()> {
        self.begin_operation()?;
        if self.pending.is_some() || symbols.len()>crate::MAX_SYMBOLS || symbols.iter().any(|s|!super::symbol_valid(s)) {return Err("invalid subscription or ACK outstanding".into())}
        let params: Vec<_>=symbols.iter().map(|s|format!("{}@bookTicker",s.to_ascii_lowercase())).collect();
        self.socket.send(Message::Text(json!({"method":"SUBSCRIBE","params":params,"id":request}).to_string().into())).map_err(|e|e.to_string())?;
        self.pending=Some(request);Ok(())
    }
    /// ACK and keepalive are returned explicitly; no silent event consumption.
    pub fn receive(&mut self) -> Result<Value> {
        match self.receive_timeout()? { Reception::Event(value)=>Ok(value),Reception::Idle=>Err("physical receive timeout".into()) }
    }
    pub fn receive_timeout(&mut self) -> Result<Reception> {
        self.begin_operation()?;
        let message=match self.socket.read() {
            Ok(message)=>message,
            Err(error) if is_timeout(&error)=>return Ok(Reception::Idle),
            Err(error)=>return Err(error.to_string()),
        };
        let result: Result<Value>=match message {
            Message::Text(raw)=>{
                self.messages+=1;self.bytes+=raw.len() as u64;
                let v: Value=crate::json::parse(raw.as_bytes()).map_err(|e|e.to_string())?;
                if let Some(id)=v.get("id").and_then(Value::as_u64) {
                    if self.pending!=Some(id) {return Err("unknown subscription ACK".into())}
                    if v.get("result")!=Some(&Value::Null) {return Err("subscription rejected".into())}
                    self.pending=None;return Ok(Reception::Event(json!({"type":"ack","id":id})));
                }
                Ok(json!({"type":"raw","payload":v}))
            },
            Message::Ping(data)=>{self.socket.flush().map_err(|e|e.to_string())?;Ok(json!({"type":"ping","bytes":data.len()}))},
            Message::Pong(data)=>Ok(json!({"type":"pong","bytes":data.len()})),
            Message::Close(_)=>Err("physical connection closed".into()),
            _=>Err("unsupported public frame".into()),
        };
        Ok(Reception::Event(result?))
    }
    pub fn close(mut self) -> Result<()> {
        self.begin_operation()?;
        self.socket.close(None).map_err(|e|e.to_string())
    }
}

#[cfg(test)]
mod endpoint_tests {
    use super::PublicSocket;
    use std::net::SocketAddr;

    #[test]
    fn endpoint_policy_is_validated_without_opening_a_socket() {
        let loopback: SocketAddr = "127.0.0.1:9001".parse().unwrap();
        let public: SocketAddr = "1.1.1.1:443".parse().unwrap();
        assert!(PublicSocket::validate_endpoint(loopback, "ws://localhost:9001/feed").is_ok());
        assert!(PublicSocket::validate_endpoint(public, "wss://fstream.binance.com/ws").is_ok());
        assert!(PublicSocket::validate_endpoint(loopback, "not a uri").is_err());
        assert!(PublicSocket::validate_endpoint(public, "wss://example.com/feed").is_err());
        assert!(PublicSocket::validate_endpoint(loopback, "http://localhost/feed").is_err());
    }
}

#[cfg(test)]
mod tls_retry_tests {
    use super::{io, Duration, Instant, MaybeTlsStream, PublicSocket, Read};
    use std::{net::TcpListener, sync::mpsc, thread};
    use tungstenite::Message;

    #[test]
    fn foundation_tls_retry_established_read_preserves_bio_error_kind() {
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();listener.set_nonblocking(true).unwrap();
        let (release,hold)=mpsc::channel::<()>();
        thread::scope(move|scope| {
            let server=scope.spawn(move||->std::result::Result<(),String> {
                let until=Instant::now()+Duration::from_secs(3);
                let stream=loop {
                    if Instant::now()>=until {return Err("retry fixture accept limit".into())}
                    match listener.accept() {
                        Ok((stream,_))=>break stream,
                        Err(error) if error.kind()==io::ErrorKind::WouldBlock=>thread::sleep(Duration::from_millis(2)),
                        Err(error)=>return Err(error.to_string()),
                    }
                };
                stream.set_read_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
                stream.set_write_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
                let mut socket=tungstenite::accept(stream).map_err(|e|e.to_string())?;
                let request=socket.read().map_err(|e|e.to_string())?.into_text().map_err(|e|e.to_string())?;
                let request:serde_json::Value=serde_json::from_str(&request).map_err(|e|e.to_string())?;
                if request!=serde_json::json!({"method":"SUBSCRIBE","params":["btcusdt@bookTicker"],"id":23}) {
                    return Err(format!("retry fixture expected actual subscription: {request}"));
                }
                socket.send(Message::Text("{\"id\":23,\"result\":null}".into())).map_err(|e|e.to_string())?;
                // Quiet established TCP stays owned until the two observations finish.
                let _=hold.recv_timeout(Duration::from_secs(3));Ok(())
            });
            let mut client=PublicSocket::connect(address,&format!("ws://localhost:{}/retry",address.port())).unwrap();
            client.subscribe(23,&["BTCUSDT".into()]).unwrap();
            assert_eq!(client.receive().unwrap(),serde_json::json!({"type":"ack","id":23}));
            client.begin_operation().unwrap();let quiet_start=Instant::now();let mut byte=[0];
            let quiet_error=match client.socket.get_mut() {
                MaybeTlsStream::Plain(stream)=>stream.read(&mut byte).unwrap_err(),
                _=>panic!("retry contract witness requires the actual plain socket stream"),
            };
            let quiet_elapsed=quiet_start.elapsed();
            client.begin_operation().unwrap();
            thread::sleep(Duration::from_millis(275));let expiry_start=Instant::now();
            let expiry_error=match client.socket.get_mut() {
                MaybeTlsStream::Plain(stream)=>stream.read(&mut byte).unwrap_err(),
                _=>panic!("retry contract witness lost its actual plain socket stream"),
            };
            let expiry_elapsed=expiry_start.elapsed();
            drop(client);let _=release.send(());server.join().unwrap().unwrap();
            assert!(quiet_elapsed>=Duration::from_millis(200)&&quiet_elapsed<=Duration::from_millis(750),
                "actual quiet TCP read must reach its 250ms timeout; elapsed={quiet_elapsed:?}, error={quiet_error:?}");
            assert!(expiry_elapsed<=Duration::from_millis(750),"expired lower IO must return promptly: {expiry_elapsed:?}");
            // This is the established TLS BIO's lower-IO contract, not TLS loss evidence.
            assert_eq!(quiet_error.kind(),io::ErrorKind::WouldBlock,
                "established quiet read must remain BIO-retryable; expiry={expiry_error:?}");
            assert_eq!(expiry_error.kind(),io::ErrorKind::WouldBlock,
                "established absolute-deadline expiry must remain BIO-retryable");

            for (phase,expected) in [
                (super::IoPhase::Establishing,io::ErrorKind::TimedOut),
                (super::IoPhase::Established,io::ErrorKind::WouldBlock),
            ] {
                for kind in [io::ErrorKind::WouldBlock,io::ErrorKind::TimedOut] {
                    assert_eq!(super::normalize_timeout(io::Error::new(kind,"phase timeout witness"),phase).kind(),expected);
                }
                for kind in [io::ErrorKind::ConnectionReset,io::ErrorKind::BrokenPipe,io::ErrorKind::NotConnected,io::ErrorKind::InvalidInput] {
                    let error=super::normalize_timeout(io::Error::new(kind,"phase IO witness"),phase);
                    assert_eq!(error.kind(),kind);
                    assert_eq!(error.to_string(),"phase IO witness");
                }
            }
            // Reuse the already expired observation instant; add no wait or horizon.
            let expiry_listener=TcpListener::bind("127.0.0.1:0").unwrap();
            let stream=super::TcpStream::connect(expiry_listener.local_addr().unwrap()).unwrap();
            let (_peer,_)=expiry_listener.accept().unwrap();
            let mut expired=super::DeadlineStream{stream,deadline:expiry_start,phase:super::IoPhase::Establishing};
            assert_eq!(expired.remaining_timeout().unwrap_err().kind(),io::ErrorKind::TimedOut,
                "establishment absolute-deadline expiry must remain TimedOut");
            expired.phase=super::IoPhase::Established;
            assert_eq!(std::io::Write::write(&mut expired,&byte).unwrap_err().kind(),io::ErrorKind::WouldBlock,
                "established expired write must remain BIO-retryable");
            assert_eq!(std::io::Write::flush(&mut expired).unwrap_err().kind(),io::ErrorKind::WouldBlock,
                "established expired flush must remain BIO-retryable");
        });
    }
}
