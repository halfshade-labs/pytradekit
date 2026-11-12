//! Real loopback TLS fixture. Keys are generated in the isolated test container,
//! never committed, and removed by the owner even when assertions unwind.
use ptk_public_market::transport::PublicSocket;
use std::{fs,net::TcpListener,path::PathBuf,process::Command,time::{Duration,SystemTime,UNIX_EPOCH}};
use tungstenite::Message;
struct Fixture(PathBuf);
impl Fixture {
    fn new()->Self {
        let id=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
        let root=std::env::temp_dir().join(format!("ptk-public-tls-{}-{id}",std::process::id()));
        fs::create_dir(&root).unwrap();
        let fixture=Self(root);
        fixture.openssl(&["req","-x509","-newkey","rsa:2048","-nodes","-keyout","key.pem","-out","ca.pem","-days","1","-subj","/CN=localhost","-addext","subjectAltName=DNS:localhost,IP:127.0.0.1"]);
        fixture.openssl(&["pkcs12","-export","-inkey","key.pem","-in","ca.pem","-out","identity.p12","-passout","pass:fixture-only"]);
        fixture
    }
    fn openssl(&self,args:&[&str]) {
        let result=Command::new("openssl").current_dir(&self.0).args(args).output().expect("openssl CLI is required in fixed TLS fixture container");
        assert!(result.status.success(),"ephemeral TLS fixture generation failed");
    }
}
impl Drop for Fixture {fn drop(&mut self){let _=fs::remove_dir_all(&self.0);}}
#[test]
fn real_tls_validates_ca_and_hostname_before_ack_and_quote(){
    let fixture=Fixture::new();
    let identity=native_tls::Identity::from_pkcs12(&fs::read(fixture.0.join("identity.p12")).unwrap(),"fixture-only").unwrap();
    let acceptor=native_tls::TlsAcceptor::new(identity).unwrap();
    let ca=fs::read(fixture.0.join("ca.pem")).unwrap();
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();
    let server=std::thread::spawn(move|| {
        for attempt in 0..3 {
            let (stream,_)=listener.accept().unwrap();stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();stream.set_write_timeout(Some(Duration::from_secs(2))).unwrap();
            let tls=acceptor.accept(stream);
            if attempt<2 {if let Ok(tls)=tls {assert!(tungstenite::accept(tls).is_err(),"invalid client must not complete WebSocket handshake");} continue}
            let mut socket=tungstenite::accept(tls.unwrap()).unwrap();
            let request=socket.read().unwrap().into_text().unwrap();let v:serde_json::Value=serde_json::from_str(&request).unwrap();assert_eq!(v["id"],7);
            socket.send(Message::Text("{\"id\":7,\"result\":null}".into())).unwrap();
            socket.send(Message::Text("{\"s\":\"BTCUSDT\",\"u\":1,\"b\":\"100\",\"a\":\"101\",\"B\":\"2\",\"A\":\"3\"}".into())).unwrap();
            socket.close(None).unwrap();
        }
    });
    let uri=format!("wss://localhost:{}/ws",address.port());
    assert!(PublicSocket::connect(address,&uri).is_err(),"self-signed CA must not be trusted by default");
    assert!(PublicSocket::connect_loopback_with_ca(address,&format!("wss://wrong.invalid:{}/ws",address.port()),&ca).is_err(),"hostname validation must remain enabled");
    let mut client=PublicSocket::connect_loopback_with_ca(address,&uri,&ca).unwrap();
    client.subscribe(7,&["BTCUSDT".into()]).unwrap();assert_eq!(client.receive().unwrap()["type"],"ack");
    let quote=client.receive().unwrap();assert_eq!(quote["payload"]["B"],"2");assert_eq!(quote["payload"]["A"],"3");
    assert!(client.receive().is_err());server.join().unwrap();
}

#[test]
fn foundation_tls_fragmented_quote_recovers_after_idle_and_quiet_read() {
    use ptk_public_market::transport::Reception;
    use std::{io::ErrorKind,sync::mpsc,thread,time::Instant};
    use tungstenite::protocol::frame::{Frame,coding::{Data,OpCode}};

    enum Step {Fragments,Finish,Quote,Done}
    let fixture=Fixture::new();
    let identity=native_tls::Identity::from_pkcs12(&fs::read(fixture.0.join("identity.p12")).unwrap(),"fixture-only").unwrap();
    let acceptor=native_tls::TlsAcceptor::new(identity).unwrap();
    let ca=fs::read(fixture.0.join("ca.pem")).unwrap();
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();
    let address=listener.local_addr().unwrap();listener.set_nonblocking(true).unwrap();
    let (control,commands)=mpsc::channel::<Step>();
    let (started,ready)=mpsc::sync_channel(1);
    let quote=serde_json::json!({"s":"BTCUSDT","u":1,"b":"100","a":"101","B":"2","A":"3"});
    let later_quote=serde_json::json!({"s":"BTCUSDT","u":2,"b":"101","a":"102","B":"4","A":"5"});
    let (fragmented,later)=(quote.clone(),later_quote.clone());
    let receive_limit=Duration::from_millis(750);
    thread::scope(move|scope| {
        let server=scope.spawn(move||->Result<(usize,Duration,Duration),String> {
            let until=Instant::now()+Duration::from_secs(3);
            let stream=loop {
                if Instant::now()>=until {return Err("TLS recovery fixture accept limit".into())}
                match listener.accept() {
                    Ok((stream,_))=>break stream,
                    Err(error) if error.kind()==ErrorKind::WouldBlock=>thread::sleep(Duration::from_millis(2)),
                    Err(error)=>return Err(error.to_string()),
                }
            };
            stream.set_read_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
            stream.set_write_timeout(Some(Duration::from_secs(2))).map_err(|e|e.to_string())?;
            let tls=acceptor.accept(stream).map_err(|e|e.to_string())?;
            let mut socket=tungstenite::accept(tls).map_err(|e|e.to_string())?;
            let request=socket.read().map_err(|e|e.to_string())?.into_text().map_err(|e|e.to_string())?;
            let request:serde_json::Value=serde_json::from_str(&request).map_err(|e|e.to_string())?;
            if request!=serde_json::json!({"method":"SUBSCRIBE","params":["btcusdt@bookTicker"],"id":29}) {
                return Err(format!("TLS recovery fixture expected actual subscription: {request}"));
            }
            let ack=serde_json::json!({"id":29,"result":null}).to_string();
            socket.send(Message::Text(ack.into())).map_err(|e|e.to_string())?;
            if !matches!(commands.recv_timeout(Duration::from_secs(3)),Ok(Step::Fragments)) {
                return Err("TLS recovery fixture expected fragment release".into());
            }
            let text=fragmented.to_string();let (prefix,suffix)=text.as_bytes().split_at(text.len()/2);
            socket.send(Message::Frame(Frame::message(prefix.to_vec(),OpCode::Data(Data::Text),false))).map_err(|e|e.to_string())?;
            let first=Instant::now();let mut last=first;let mut writes=1;let mut max_gap=Duration::ZERO;let mut finish=false;
            started.send(()).map_err(|e|e.to_string())?;
            // Real WebSocket frames pass through TLS; encrypted TLS records are not split deliberately.
            while !finish||first.elapsed()<Duration::from_millis(350) {
                if first.elapsed()>=Duration::from_secs(2) {return Err("TLS recovery fixture finite fragment horizon".into())}
                match commands.recv_timeout(Duration::from_millis(20)) {
                    Ok(Step::Finish)=>finish=true,
                    Err(mpsc::RecvTimeoutError::Timeout)=>{},
                    _=>return Err("TLS recovery fixture fragment command disconnected or out of order".into()),
                }
                socket.send(Message::Frame(Frame::message(Vec::<u8>::new(),OpCode::Data(Data::Continue),false))).map_err(|e|e.to_string())?;
                let now=Instant::now();max_gap=max_gap.max(now.duration_since(last));last=now;writes+=1;
            }
            let span=last.duration_since(first);
            socket.send(Message::Frame(Frame::message(suffix.to_vec(),OpCode::Data(Data::Continue),true))).map_err(|e|e.to_string())?;
            if !matches!(commands.recv_timeout(Duration::from_secs(3)),Ok(Step::Quote)) {
                return Err("TLS recovery fixture expected later quote release".into());
            }
            socket.send(Message::Text(later.to_string().into())).map_err(|e|e.to_string())?;
            if !matches!(commands.recv_timeout(Duration::from_secs(3)),Ok(Step::Done)) {
                return Err("TLS recovery fixture expected completion".into());
            }
            Ok((writes,span,max_gap))
        });
        let uri=format!("wss://localhost:{}/recovery",address.port());
        let mut client=PublicSocket::connect_loopback_with_ca(address,&uri,&ca).unwrap();
        client.subscribe(29,&["BTCUSDT".into()]).unwrap();
        assert_eq!(client.receive().unwrap(),serde_json::json!({"type":"ack","id":29}));
        let ack_bytes=serde_json::json!({"id":29,"result":null}).to_string().len() as u64;
        assert_eq!((client.messages,client.bytes),(1,ack_bytes));
        control.send(Step::Fragments).unwrap();ready.recv_timeout(Duration::from_secs(3)).unwrap();
        let start=Instant::now();let idle=client.receive_timeout();let elapsed=start.elapsed();
        assert!(matches!(&idle,Ok(Reception::Idle))&&elapsed<=receive_limit,
            "unfinished real WSS message must yield Idle within 750ms total observation of its 250ms IO budget; elapsed={elapsed:?}, error={:?}",idle.as_ref().err());
        assert_eq!((client.messages,client.bytes),(1,ack_bytes));
        control.send(Step::Finish).unwrap();
        assert_eq!(client.receive().unwrap(),serde_json::json!({"type":"raw","payload":quote}));
        let recovered_bytes=ack_bytes+quote.to_string().len() as u64;
        assert_eq!((client.messages,client.bytes),(2,recovered_bytes));
        let quiet_start=Instant::now();let quiet=client.receive_timeout();let quiet_elapsed=quiet_start.elapsed();
        assert!(matches!(&quiet,Ok(Reception::Idle))&&quiet_elapsed<=receive_limit,
            "quiet established WSS read must yield Idle; elapsed={quiet_elapsed:?}, error={:?}",quiet.as_ref().err());
        assert_eq!((client.messages,client.bytes),(2,recovered_bytes));
        control.send(Step::Quote).unwrap();
        assert_eq!(client.receive().unwrap(),serde_json::json!({"type":"raw","payload":later_quote}));
        assert_eq!((client.messages,client.bytes),(3,recovered_bytes+later_quote.to_string().len() as u64));
        control.send(Step::Done).unwrap();drop(client);
        let (writes,span,max_gap)=server.join().unwrap().unwrap();
        assert!(writes>=2&&span>=Duration::from_millis(350)&&max_gap<Duration::from_millis(200),
            "WSS fixture must hold unfinished message beyond 250ms with continuous progress; writes={writes}, span={span:?}, max_gap={max_gap:?}");
    });
}
