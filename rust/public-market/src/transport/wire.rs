//! Bounded WebSocket I/O shared by public-market protocol adapters.
use crate::{Result, MAX_LINE};
use std::io::{self, Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::{Duration, Instant};
use tungstenite::protocol::WebSocketConfig;
use tungstenite::{client_tls_with_config, stream::MaybeTlsStream, Message, WebSocket};

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

pub(super) struct Wire {
    socket: WebSocket<MaybeTlsStream<DeadlineStream>>,
}

impl Wire {
    /// The adapter owns its hostname policy. Plain and custom-TLS sockets are local fixtures.
    pub(super) fn connect(
        address: SocketAddr,
        request: tungstenite::handshake::client::Request,
        connector: Option<tungstenite::Connector>,
    ) -> Result<Self> {
        let scheme = request.uri().scheme_str();
        if !(scheme == Some("wss") || (scheme == Some("ws") && address.ip().is_loopback())) {
            return Err("unapproved websocket transport scheme".into());
        }
        if connector.is_some() && !address.ip().is_loopback() {
            return Err("custom websocket TLS requires loopback address".into());
        }
        let deadline = checked_deadline(CONNECT_TIMEOUT).map_err(|error| error.to_string())?;
        let timeout = remaining_time(deadline).map_err(|error| error.to_string())?;
        let stream = TcpStream::connect_timeout(&address, timeout).map_err(|error| error.to_string())?;
        let stream = DeadlineStream { stream, deadline, phase: IoPhase::Establishing };
        let config = WebSocketConfig::default()
            .max_message_size(Some(MAX_LINE))
            .max_frame_size(Some(MAX_LINE))
            .write_buffer_size(16384)
            .max_write_buffer_size(MAX_LINE * 2);
        let (socket, _) = client_tls_with_config(request, stream, Some(config), connector)
            .map_err(|error| error.to_string())?;
        remaining_time(deadline).map_err(|error| error.to_string())?;
        Ok(Self { socket })
    }

    fn begin_operation(&mut self) -> Result<()> {
        let stream = match self.socket.get_mut() {
            MaybeTlsStream::Plain(stream) => stream,
            MaybeTlsStream::NativeTls(stream) => stream.get_mut(),
            _ => return Err("unsupported physical socket stream".into()),
        };
        stream.reset_deadline().map_err(|error| error.to_string())
    }

    pub(super) fn send(&mut self, message: impl FnOnce() -> Result<Message>) -> Result<()> {
        self.begin_operation()?;
        self.socket.send(message()?).map_err(|error| error.to_string())
    }

    pub(super) fn read(&mut self) -> Result<Option<Message>> {
        self.begin_operation()?;
        match self.socket.read() {
            Ok(message) => {
                if matches!(message, Message::Ping(_)) {
                    // Tungstenite queued the Pong. Flush it under the same read deadline.
                    self.socket.flush().map_err(|error| error.to_string())?;
                }
                Ok(Some(message))
            }
            Err(error) if is_timeout(&error) => Ok(None),
            Err(error) => Err(error.to_string()),
        }
    }

    pub(super) fn close(&mut self) -> Result<()> {
        self.begin_operation()?;
        self.socket.close(None).map_err(|error| error.to_string())
    }
}

#[cfg(test)]
mod tls_retry_tests {
    use super::super::PublicSocket;
    use super::{io, Duration, Instant, MaybeTlsStream, Read, Wire};
    use std::{net::TcpListener, sync::mpsc, thread};
    use tungstenite::client::IntoClientRequest;
    use tungstenite::Message;

    #[test]
    fn plain_websocket_is_restricted_to_loopback_before_dial() {
        let remote = "192.0.2.1:80".parse().unwrap();
        let request = "ws://example.test/".into_client_request().unwrap();
        assert!(matches!(Wire::connect(remote, request, None),
            Err(error) if error == "unapproved websocket transport scheme"));
    }

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
            client.wire.begin_operation().unwrap();let quiet_start=Instant::now();let mut byte=[0];
            let quiet_error=match client.wire.socket.get_mut() {
                MaybeTlsStream::Plain(stream)=>stream.read(&mut byte).unwrap_err(),
                _=>panic!("retry contract witness requires the actual plain socket stream"),
            };
            let quiet_elapsed=quiet_start.elapsed();
            client.wire.begin_operation().unwrap();
            thread::sleep(Duration::from_millis(275));let expiry_start=Instant::now();
            let expiry_error=match client.wire.socket.get_mut() {
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
