//! Binance public-market messages and subscriptions over the bounded WebSocket transport.
use crate::Result;
use serde_json::{json, Value};
use std::net::SocketAddr;
use tungstenite::{client::IntoClientRequest, Message};

mod wire;
use wire::Wire;

pub enum Reception { Event(Value), Idle }

pub(crate) enum TextDecode { Received, Failed }

pub struct PublicSocket {
    wire: Wire,
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
        let request = uri.into_client_request().map_err(|error| error.to_string())?;
        let wire = Wire::connect(address, request, connector)?;
        Ok(Self { wire, pending: None, messages: 0, bytes: 0 })
    }
    pub fn subscribe(&mut self,request: u64,symbols: &[String]) -> Result<()> {
        self.wire.send(|| {
            if self.pending.is_some() || symbols.len()>crate::MAX_SYMBOLS || symbols.iter().any(|s|!super::symbol_valid(s)) {
                return Err("invalid subscription or ACK outstanding".into());
            }
            let params: Vec<_> = symbols.iter().map(|s| format!("{}@bookTicker", s.to_ascii_lowercase())).collect();
            Ok(Message::Text(json!({"method":"SUBSCRIBE","params":params,"id":request}).to_string().into()))
        })?;
        self.pending=Some(request);Ok(())
    }
    /// ACK and keepalive are returned explicitly; no silent event consumption.
    pub fn receive(&mut self) -> Result<Value> {
        match self.receive_timeout()? { Reception::Event(value)=>Ok(value),Reception::Idle=>Err("physical receive timeout".into()) }
    }
    pub fn receive_timeout(&mut self) -> Result<Reception> {
        self.receive_timeout_observed(|_| {})
    }

    pub(crate) fn receive_timeout_observed(&mut self, mut observe: impl FnMut(TextDecode)) -> Result<Reception> {
        let Some(message) = self.wire.read()? else {
            return Ok(Reception::Idle);
        };
        let result: Result<Value>=match message {
            Message::Text(raw)=>{
                self.messages+=1;self.bytes+=raw.len() as u64;
                observe(TextDecode::Received);
                let v: Value=crate::json::parse(raw.as_bytes()).map_err(|error| {
                    observe(TextDecode::Failed);
                    error.to_string()
                })?;
                if let Some(id)=v.get("id").and_then(Value::as_u64) {
                    if self.pending!=Some(id) {return Err("unknown subscription ACK".into())}
                    if v.get("result")!=Some(&Value::Null) {return Err("subscription rejected".into())}
                    self.pending=None;return Ok(Reception::Event(json!({"type":"ack","id":id})));
                }
                Ok(json!({"type":"raw","payload":v}))
            },
            Message::Ping(data)=>Ok(json!({"type":"ping","bytes":data.len()})),
            Message::Pong(data)=>Ok(json!({"type":"pong","bytes":data.len()})),
            Message::Close(_)=>Err("physical connection closed".into()),
            _=>Err("unsupported public frame".into()),
        };
        Ok(Reception::Event(result?))
    }
    pub fn close(mut self) -> Result<()> {
        self.wire.close()
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
