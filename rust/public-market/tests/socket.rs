use ptk_public_market::transport::PublicSocket;
use std::net::TcpListener;
use tungstenite::Message;
#[test]
fn actual_socket_handshake_subscription_ack_ping_quote_and_close(){
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move||{
        let (stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(std::time::Duration::from_secs(2))).unwrap();
        let mut socket=tungstenite::accept(stream).unwrap();
        let request=socket.read().unwrap().into_text().unwrap();let v:serde_json::Value=serde_json::from_str(&request).unwrap();assert_eq!(v["params"][0],"btcusdt@bookTicker");
        socket.send(Message::Text("{\"id\":1,\"result\":null}".into())).unwrap();
        socket.send(Message::Ping(vec![1,2].into())).unwrap();
        socket.send(Message::Text("{\"s\":\"BTCUSDT\",\"b\":\"100\",\"a\":\"101\",\"B\":\"2\",\"A\":\"3\"}".into())).unwrap();
        assert!(matches!(socket.read().unwrap(),Message::Pong(_)));socket.close(None).unwrap();
    });
    let mut client=PublicSocket::connect(address,&format!("ws://localhost:{}/ws",address.port())).unwrap();client.subscribe(1,&["BTCUSDT".into()]).unwrap();
    assert_eq!(client.receive().unwrap()["type"],"ack");assert_eq!(client.receive().unwrap()["type"],"ping");assert_eq!(client.receive().unwrap()["payload"]["B"],"2");assert!(client.receive().is_err());server.join().unwrap();
}

mod foundation_wire_tests {
    use super::*;
    use ptk_public_market::MAX_LINE;
    use serde_json::{json,Value};
    use std::sync::{Arc,atomic::{AtomicBool,Ordering}};
    use std::time::Duration;
    use tungstenite::protocol::frame::{Frame,coding::{Data,OpCode}};

    #[derive(Clone,Copy,Debug)]
    enum WireCase {Inclusive,SingleOverflow,FragmentedOverflow}
    struct WireFixture {
        address:std::net::SocketAddr,stop:Arc<AtomicBool>,
        join:Option<std::thread::JoinHandle<Result<(),String>>>,
    }
    impl WireFixture {
        fn start(case:WireCase)->Self {
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
            let address=listener.local_addr().unwrap();let stop=Arc::new(AtomicBool::new(false));let server_stop=stop.clone();
            let join=std::thread::spawn(move|| {
                let stream=loop {
                    if server_stop.load(Ordering::Acquire){return Ok(())}
                    match listener.accept(){Ok((stream,_))=>break stream,
                        Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(2)),
                        Err(e)=>return Err(e.to_string()),}
                };
                stream.set_read_timeout(Some(Duration::from_millis(500))).map_err(|e|e.to_string())?;
                stream.set_write_timeout(Some(Duration::from_millis(500))).map_err(|e|e.to_string())?;
                let mut socket=tungstenite::accept(stream).map_err(|e|e.to_string())?;
                let request:Value=serde_json::from_str(&socket.read().map_err(|e|e.to_string())?.into_text().map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
                if request!=json!({"method":"SUBSCRIBE","params":["btcusdt@bookTicker"],"id":7}) {return Err(format!("unexpected request: {request}"))}
                socket.send(Message::Text(json!({"id":request["id"],"result":null}).to_string().into())).map_err(|e|e.to_string())?;
                send_payload(&mut socket,case)?;
                socket.get_mut().set_read_timeout(Some(Duration::from_millis(10))).map_err(|e|e.to_string())?;
                while !server_stop.load(Ordering::Acquire) {
                    match socket.read(){Err(tungstenite::Error::Io(e)) if matches!(e.kind(),std::io::ErrorKind::TimedOut|std::io::ErrorKind::WouldBlock)=>{},
                        Ok(Message::Close(_))|Err(_)=>break,_=>{},}
                }
                Ok(())
            });
            Self{address,stop,join:Some(join)}
        }
        fn finish(&mut self)->Result<(),String> {
            self.stop.store(true,Ordering::Release);
            self.join.take().map_or(Ok(()),|join|join.join().unwrap_or_else(|_|Err("wire fixture panicked".into())))
        }
    }
    impl Drop for WireFixture {fn drop(&mut self){let _=self.finish();}}

    fn wire_payload(size:usize)->String {
        let mut text=json!({"s":"BTCUSDT","b":"100","a":"101","B":"2","A":"3"}).to_string();
        text.push_str(&" ".repeat(size-text.len()));text
    }
    fn send_payload(socket:&mut tungstenite::WebSocket<std::net::TcpStream>,case:WireCase)->Result<(),String> {
        let text=wire_payload(match case{WireCase::Inclusive=>MAX_LINE,_=>MAX_LINE+1});
        match case {
            WireCase::FragmentedOverflow=>{
                let (first,last)=text.as_bytes().split_at(MAX_LINE/2);
                assert!(first.len()<=MAX_LINE&&last.len()<=MAX_LINE);
                socket.send(Message::Frame(Frame::message(first.to_vec(),OpCode::Data(Data::Text),false))).map_err(|e|e.to_string())?;
                socket.send(Message::Frame(Frame::message(last.to_vec(),OpCode::Data(Data::Continue),true))).map_err(|e|e.to_string())
            },
            _=>socket.send(Message::Text(text.into())).map_err(|e|e.to_string()),
        }
    }

    #[test]
    fn foundation_socket_enforces_inclusive_frame_and_aggregate_message_limits() {
        for case in [WireCase::Inclusive,WireCase::SingleOverflow,WireCase::FragmentedOverflow] {
            let mut fixture=WireFixture::start(case);
            let mut client=PublicSocket::connect(fixture.address,&format!("ws://localhost:{}/wire",fixture.address.port())).unwrap();
            client.subscribe(7,&["BTCUSDT".into()]).unwrap();
            assert_eq!(client.receive().unwrap(),json!({"type":"ack","id":7}));
            let ack_bytes=json!({"id":7,"result":null}).to_string().len() as u64;
            assert_eq!((client.messages,client.bytes),(1,ack_bytes));
            let result=client.receive();
            match case {
                WireCase::Inclusive=>{
                    assert_eq!(result.unwrap(),json!({"type":"raw","payload":{"s":"BTCUSDT","b":"100","a":"101","B":"2","A":"3"}}));
                    assert_eq!((client.messages,client.bytes),(2,ack_bytes+MAX_LINE as u64));
                },
                _=>{
                    assert_eq!(result.unwrap_err(),format!("Space limit exceeded: Message too long: {} > {}",MAX_LINE+1,MAX_LINE),"{case:?}");
                    assert_eq!((client.messages,client.bytes),(1,ack_bytes),"rejected wire must not become a text message: {case:?}");
                },
            }
            drop(client);fixture.finish().unwrap();
        }
    }
}
