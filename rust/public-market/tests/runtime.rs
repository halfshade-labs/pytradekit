use ptk_public_market::runtime::{Runtime,Settings};
use serde_json::{json,Value};
use std::net::TcpListener;
use std::sync::{Arc,atomic::{AtomicBool,Ordering}};
use std::time::{Duration,Instant,SystemTime,UNIX_EPOCH};
use tungstenite::Message;

#[test]
fn runtime_rejects_invalid_endpoint_before_returning_a_running_owner() {
    let settings: Settings = serde_json::from_value(json!({
        "session": "invalid-endpoint",
        "mode": "single",
        "perpetual": {"address": "127.0.0.1:9001", "uri": "not a websocket URI"},
        "spot": {"address": "127.0.0.1:9002", "uri": "ws://localhost:9002/spot"},
        "perpetual_symbols": ["BTCUSDT"],
        "spot_symbols": ["BTCUSDT"]
    }))
    .unwrap();
    assert!(Runtime::start(settings).is_err());
}

#[test]
fn actual_workers_ack_reconnect_rotate_and_join_exactly(){
    let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
    let address=listener.local_addr().unwrap();let stopped=Arc::new(AtomicBool::new(false));let stop=stopped.clone();
    let server=std::thread::spawn(move||{
        let mut clients=Vec::new();let mut connection=0;let closed_perpetual=Arc::new(AtomicBool::new(false));
        while !stop.load(Ordering::Acquire){
            let (stream,_)=match listener.accept(){Ok(v)=>v,Err(e)if e.kind()==std::io::ErrorKind::WouldBlock=>{std::thread::sleep(Duration::from_millis(5));continue},Err(e)=>panic!("{e}")};
            connection+=1;let id=connection;let stop=stop.clone();let closed_perpetual=closed_perpetual.clone();
            clients.push(std::thread::spawn(move||{
                stream.set_read_timeout(Some(Duration::from_millis(100))).unwrap();
                let mut perpetual=false;
                let mut socket=tungstenite::accept_hdr(stream,|request:&tungstenite::handshake::server::Request,response|{
                    perpetual=request.uri().path()=="/perp";Ok(response)
                }).unwrap();
                let request:Value=serde_json::from_str(&socket.read().unwrap().into_text().unwrap()).unwrap();
                socket.send(Message::Text(json!({"id":request["id"],"result":null}).to_string().into())).unwrap();
                let quote=json!({"s":"BTCUSDT","u":id,"E":SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64,"b":"100","a":"101","B":"2","A":"3"});
                socket.send(Message::Text(quote.to_string().into())).unwrap();
                // Force the primary perpetual generation to reconnect before
                // rotation. Connection arrival order never identifies its slot.
                if perpetual&&!closed_perpetual.swap(true,Ordering::AcqRel) {let _=socket.close(None);return}
                while !stop.load(Ordering::Acquire) {match socket.read(){Err(tungstenite::Error::Io(e))if matches!(e.kind(),std::io::ErrorKind::TimedOut|std::io::ErrorKind::WouldBlock)=>{},Ok(Message::Close(_))|Err(_)=>break,_=>{}}}
            }));
        }
        for client in clients {client.join().unwrap();}
    });
    let settings:Settings=serde_json::from_value(json!({"session":"test","mode":"single",
        "perpetual":{"address":address,"uri":format!("ws://localhost:{}/perp",address.port())},
        "spot":{"address":address,"uri":format!("ws://localhost:{}/spot",address.port())},
        "perpetual_symbols":["BTCUSDT"],"spot_symbols":["BTCUSDT"]})).unwrap();
    let mut runtime=Runtime::start(settings).unwrap();let deadline=Instant::now()+Duration::from_secs(4);
    let mut selector=ptk_public_market::Selector::default();selector.apply(serde_json::from_value(json!({"type":"configure","stream_id":"test","mode":"single","symbols":["BTCUSDT"],"wall_ms":SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64,"monotonic_ms":0})).unwrap()).unwrap();
    let mut reconnected=false;let mut rotated=false;let mut promoted=false;let mut acked=0;
    let mut original_token=None;let mut replacement_route=None;
    while Instant::now()<deadline {
        for row in runtime.recv(Duration::from_millis(20)).unwrap(){
            if row["type"]=="feed" {let action=serde_json::from_value(row["action"].clone()).unwrap();selector.apply(action).unwrap();}
            if row["type"]=="feed"&&row["action"]["type"]=="route"&&row["action"]["connected"]==true{
                acked+=1;
                let action=&row["action"];
                if !rotated&&action["generation"]==1 {original_token=Some(action["token"].clone());}
                if !rotated&&action["generation"].as_u64().unwrap()>1{
                    assert_ne!(original_token.as_ref().unwrap(),&action["token"]);reconnected=true;
                }
                if action["replacement"]==true {replacement_route=action["route"].as_str().map(str::to_owned);}
                if replacement_route.as_deref()==action["route"].as_str()&&action["replacement"]==false{promoted=true;}
            }
        }
        let clock=runtime.clock();selector.apply(ptk_public_market::Action::Poll{output_capacity:4096,clock}).unwrap();
        for row in selector.diagnostics()["routes"].as_array().unwrap(){let id=row["route"].as_str().unwrap();if row["replacement"]==true&&selector.replacement_ready(id,clock).unwrap(){*runtime.control().promotion.lock().unwrap()=Some(id.into());}}
        if !rotated&&reconnected {runtime.rotate(0).unwrap();rotated=true;}
        if promoted&&reconnected{break}
    }
    runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
    stopped.store(true,Ordering::Release);server.join().unwrap();
    assert!(acked>=3);assert!(reconnected&&rotated&&promoted);
}

// Stage A: exercise the existing socket/ACK/Runtime API, without a new policy API.
#[cfg(test)]
mod native_bn_decimal_string_tests {
    use super::*;
    use std::sync::Mutex;

    const STRICT_ERROR:&str="native BN bookTicker requires decimal-string prices and sizes";

    fn string_fields()->[String;4] {
        ["\"100.00000000\"","\"101.00000000\"","\"2.00\"","\"3.00\""].map(str::to_owned)
    }
    fn numeric_cells()->Vec<(String,[String;4])> {
        let names=["b","a","B","A"];
        let tokens=[["100","100.0","1e2"],["101","101.0","1.01e2"],["2","2.0","2e0"],["3","3.0","3e0"]];
        let mut cells=Vec::new();
        for field in 0..4 {for kind in 0..3 {
            let mut fields=string_fields();fields[field]=tokens[field][kind].into();
            cells.push((format!("{}-{}",names[field],["integer","decimal","exponent"][kind]),fields));
        }}
        cells.push(("mixed".into(),["100","1.01e2","2.0","3e0"].map(str::to_owned)));cells
    }
    fn literal_book(fields:&[String;4],sequence:u64)->String {
        let source=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis();
        format!("{{\"s\":\"BTCUSDT\",\"u\":{sequence},\"E\":{source},\"b\":{},\"a\":{},\"B\":{},\"A\":{}}}",fields[0],fields[1],fields[2],fields[3])
    }

    struct WireFixture {
        settings:Settings,release:Arc<AtomicBool>,stop:Arc<AtomicBool>,
        errors:Arc<Mutex<Vec<String>>>,join:Option<std::thread::JoinHandle<usize>>,
    }
    impl WireFixture {
        fn start(role:&str,fields:[String;4])->Self {
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
            let address=listener.local_addr().unwrap();let target=role.to_owned();
            let stop=Arc::new(AtomicBool::new(false));let release=Arc::new(AtomicBool::new(false));let errors=Arc::new(Mutex::new(Vec::new()));
            let (server_stop,server_release,server_errors)=(stop.clone(),release.clone(),errors.clone());
            let join=std::thread::spawn(move||{
                let mut peers=Vec::new();
                while !server_stop.load(Ordering::Acquire) {
                    let (stream,_)=match listener.accept() {
                        Ok(pair)=>pair,Err(error) if error.kind()==std::io::ErrorKind::WouldBlock=>{std::thread::sleep(Duration::from_millis(2));continue},
                        Err(error)=>{server_errors.lock().unwrap().push(error.to_string());break},
                    };
                    let (stop,release,errors,target,fields)=(server_stop.clone(),server_release.clone(),server_errors.clone(),target.clone(),fields.clone());
                    peers.push(std::thread::spawn(move||{
                        let served=(||->std::result::Result<(),String>{
                            stream.set_read_timeout(Some(Duration::from_millis(500))).map_err(|e|e.to_string())?;
                            stream.set_write_timeout(Some(Duration::from_millis(500))).map_err(|e|e.to_string())?;
                            let mut path=String::new();
                            let mut socket=tungstenite::accept_hdr(stream,|request:&tungstenite::handshake::server::Request,response|{path=request.uri().path().to_owned();Ok(response)}).map_err(|e|e.to_string())?;
                            let request:Value=serde_json::from_str(&socket.read().map_err(|e|e.to_string())?.into_text().map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
                            if request["method"]!="SUBSCRIBE"||request["params"]!=json!(["btcusdt@bookTicker"]) {return Err("unexpected subscription".into())}
                            socket.send(Message::Text(json!({"id":request["id"],"result":null}).to_string().into())).map_err(|e|e.to_string())?;
                            socket.get_mut().set_read_timeout(Some(Duration::from_millis(10))).map_err(|e|e.to_string())?;
                            let selected=path==format!("/{target}");let mut sent=false;
                            if selected {let wire=literal_book(&string_fields(),1);eprintln!("NATIVE_BN_LITERAL_WIRE={}",json!({"role":target.as_str(),"sequence":1,"wire":wire.as_str()}));socket.send(Message::Text(wire.into())).map_err(|e|e.to_string())?;}
                            while !stop.load(Ordering::Acquire) {
                                if selected&&!sent&&release.load(Ordering::Acquire) {let wire=literal_book(&fields,2);eprintln!("NATIVE_BN_LITERAL_WIRE={}",json!({"role":target.as_str(),"sequence":2,"wire":wire.as_str()}));socket.send(Message::Text(wire.into())).map_err(|e|e.to_string())?;sent=true;}
                                match socket.read() {
                                    Err(tungstenite::Error::Io(e)) if matches!(e.kind(),std::io::ErrorKind::TimedOut|std::io::ErrorKind::WouldBlock)=>{},
                                    Ok(Message::Close(_))|Err(tungstenite::Error::ConnectionClosed)|Err(tungstenite::Error::AlreadyClosed)=>break,
                                    Err(error)=>{if !stop.load(Ordering::Acquire){return Err(error.to_string())}break},_=>{},
                                }
                            }Ok(())
                        })();
                        if let Err(error)=served {errors.lock().unwrap().push(error);}
                    }));
                }
                let count=peers.len();for peer in peers {peer.join().unwrap();}count
            });
            let settings=serde_json::from_value(json!({"session":"native-numeric-stage-a","mode":"single",
                "perpetual":{"address":address,"uri":format!("ws://localhost:{}/perp",address.port())},
                "spot":{"address":address,"uri":format!("ws://localhost:{}/spot",address.port())},
                "perpetual_symbols":["BTCUSDT"],"spot_symbols":["BTCUSDT"]})).unwrap();
            Self{settings,release,stop,errors,join:Some(join)}
        }
        fn finish(&mut self)->(usize,Vec<String>) {
            self.stop.store(true,Ordering::Release);
            let peers=self.join.take().map(|join|join.join().unwrap()).unwrap_or(0);
            (peers,self.errors.lock().unwrap().clone())
        }
    }
    impl Drop for WireFixture {fn drop(&mut self){if self.join.is_some(){let _=self.finish();}}}

    fn raw_payload(row:&Value)->Option<&Value> {
        if row["type"]=="spot_raw" {Some(&row["payload"])}
        else if row["action"]["type"]=="raw_captured" {Some(&row["action"]["payload"])}else{None}
    }
    fn next_raw(runtime:&mut Runtime,deadline:Instant)->std::result::Result<Value,String> {
        while Instant::now()<deadline {for row in runtime.recv(Duration::from_millis(10))? {if raw_payload(&row).is_some(){return Ok(row)}}}
        Err("fixture raw deadline".into())
    }
    fn run_cell(role:&str,fields:[String;4],configured:bool,deadline:Instant)->(std::result::Result<Value,String>,Value,u64) {
        let mut fixture=WireFixture::start(role,fields.clone());
        let mut runtime=if configured {Runtime::start_configured(fixture.settings.clone())}else{Runtime::start(fixture.settings.clone())}.unwrap();
        let baseline=next_raw(&mut runtime,deadline).expect("actual ACK/string barrier must succeed before numeric substitution");
        let original=raw_payload(&baseline).unwrap();assert_eq!(original["u"],1);
        for (field,text) in ["b","a","B","A"].into_iter().zip(["100.00000000","101.00000000","2.00","3.00"]) {assert_eq!(original[field],text);}
        if role=="spot" {assert!(baseline["token"].as_str().is_some_and(|s|!s.is_empty()));assert_eq!(baseline["generation"],1);}
        else {assert_eq!(baseline["action"]["type"],"raw_captured");assert!(original["_ptk_connection_id"].as_str().is_some_and(|s|!s.is_empty()));assert_eq!(original["_ptk_connection_generation"],1);}
        let supplied=runtime.accounting()["supplied"].as_u64().unwrap();assert_eq!(supplied,1);
        fixture.release.store(true,Ordering::Release);let result=next_raw(&mut runtime,deadline);
        if let Ok(row)=&result {
            let payload=raw_payload(row).unwrap();assert_eq!(payload["u"],2);assert!(payload["E"].as_i64().is_some_and(|stamp|stamp>0));
            if role=="spot" {assert_eq!(row["token"],baseline["token"]);assert_eq!(row["generation"],baseline["generation"]);}
            else {assert_eq!(payload["_ptk_connection_id"],original["_ptk_connection_id"]);assert_eq!(payload["_ptk_connection_generation"],original["_ptk_connection_generation"]);}
        }
        let after=runtime.accounting();
        // Cleanup is measured independently from the admission expectation.
        let cleanup_deadline=Instant::now()+Duration::from_secs(2);
        loop {let _=runtime.shutdown();if runtime.accounting()["workers"]==0{break}assert!(Instant::now()<cleanup_deadline,"runtime retained a worker");std::thread::sleep(Duration::from_millis(5));}
        assert_eq!(runtime.accounting()["workers"],0);drop(runtime);
        let (peers,errors)=fixture.finish();assert_eq!(peers,2);assert!(errors.is_empty(),"fixture errors: {errors:?}");
        (result,after,supplied)
    }

    #[test]
    fn configured_runtime_rejects_numeric_bookticker_before_supply() {
        let deadline=Instant::now()+Duration::from_secs(40);let mut failures=Vec::new();let mut cells=0;
        for role in ["perp","spot"] {for (name,fields) in numeric_cells() {
            let (result,after,supplied)=run_cell(role,fields,true,deadline);cells+=1;
            if result.as_ref().err().map(String::as_str)!=Some(STRICT_ERROR)||after["supplied"]!=supplied {
                failures.push(format!("{role}/{name}: result={result:?}; supplied_before={supplied}; after={}",after["supplied"]));
            }
        }}
        eprintln!("NATIVE_BN_CONFIGURED_NUMERIC_CELLS={cells}");assert_eq!(cells,26);assert!(failures.is_empty(),"{}",failures.join("\n"));
    }
    #[test]
    fn runtime_string_text_and_shared_legacy_numeric_contract_are_unchanged() {
        let deadline=Instant::now()+Duration::from_secs(40);let mut numeric=0;let mut strings=0;
        for role in ["perp","spot"] {
            let (row,after,_)=run_cell(role,string_fields(),true,deadline);let row=row.unwrap();let payload=raw_payload(&row).unwrap();
            for (field,text) in ["b","a","B","A"].into_iter().zip(["100.00000000","101.00000000","2.00","3.00"]) {assert_eq!(payload[field],text);}
            assert_eq!(after["supplied"],2);strings+=1;
            for (_,fields) in numeric_cells() {
                let (row,after,_)=run_cell(role,fields.clone(),false,deadline);let row=row.unwrap();let payload=raw_payload(&row).unwrap();
                for (field,token) in ["b","a","B","A"].into_iter().zip(fields) {assert_eq!(payload[field],serde_json::from_str::<Value>(&token).unwrap());}
                assert_eq!(payload["u"],2);assert_eq!(after["supplied"],2);numeric+=1;
            }
        }
        eprintln!("NATIVE_BN_STRING_CONTROL_CELLS={strings};NATIVE_BN_LEGACY_NUMERIC_CELLS={numeric}");assert_eq!((strings,numeric),(2,26));
    }
}

mod foundation_runtime_tests {
    use super::*;
    use ptk_public_market::{Action,Selector};
    use ptk_public_market::runtime::handoff::{Phase,Targets};
    use std::collections::{BTreeMap,BTreeSet};
    use std::sync::{Mutex,atomic::AtomicUsize};

    const PRE_ACK_FAILURE:&str="retired 1 pre-ACK frames; owner incomplete";
    #[derive(Clone,Copy)]
    enum Scenario {PreAck,ChangedTargets}
    struct PeerControl {
        scenario:Scenario,stop:Arc<AtomicBool>,release:Arc<AtomicBool>,
        records:Arc<Mutex<Vec<Value>>>,errors:Arc<Mutex<Vec<String>>>,accepted:Arc<AtomicUsize>,
    }
    impl Clone for PeerControl {
        fn clone(&self)->Self {Self{scenario:self.scenario,stop:self.stop.clone(),release:self.release.clone(),
            records:self.records.clone(),errors:self.errors.clone(),accepted:self.accepted.clone()}}
    }
    struct RuntimeFixture {settings:Settings,peers:PeerControl,join:Option<std::thread::JoinHandle<()>>}
    impl RuntimeFixture {
        fn start(scenario:Scenario)->Self {
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();listener.set_nonblocking(true).unwrap();
            let address=listener.local_addr().unwrap();
            let peers=PeerControl{scenario,stop:Arc::new(AtomicBool::new(false)),release:Arc::new(AtomicBool::new(false)),
                records:Arc::new(Mutex::new(Vec::new())),errors:Arc::new(Mutex::new(Vec::new())),accepted:Arc::new(AtomicUsize::new(0))};
            let server=peers.clone();
            let join=std::thread::spawn(move|| {
                let mut threads=Vec::new();
                while !server.stop.load(Ordering::Acquire) {
                    match listener.accept() {
                        Ok((stream,_))=>{
                            server.accepted.fetch_add(1,Ordering::AcqRel);let peer=server.clone();
                            threads.push(std::thread::spawn(move||serve_peer(stream,peer)));
                        },
                        Err(e) if e.kind()==std::io::ErrorKind::WouldBlock=>std::thread::sleep(Duration::from_millis(2)),
                        Err(e)=>{server.errors.lock().unwrap().push(e.to_string());break},
                    }
                }
                for thread in threads {if thread.join().is_err(){server.errors.lock().unwrap().push("peer panicked".into());}}
            });
            let settings=serde_json::from_value(json!({"session":"foundation-runtime","mode":"single",
                "perpetual":{"address":address,"uri":format!("ws://localhost:{}/perp",address.port())},
                "spot":{"address":address,"uri":format!("ws://localhost:{}/spot",address.port())},
                "perpetual_symbols":["BTCUSDT"],"spot_symbols":["BTCUSDT"]})).unwrap();
            Self{settings,peers,join:Some(join)}
        }
        fn records(&self)->Vec<Value>{self.peers.records.lock().unwrap().clone()}
        fn finish(&mut self) {
            self.peers.stop.store(true,Ordering::Release);self.peers.release.store(true,Ordering::Release);
            if let Some(join)=self.join.take(){if join.join().is_err(){self.peers.errors.lock().unwrap().push("listener panicked".into());}}
        }
        fn assert_clean(&self,count:usize) {
            assert_eq!(self.peers.accepted.load(Ordering::Acquire),count);
            assert_eq!(self.records().len(),count);assert!(self.records().iter().all(|r|r["closed"]==true));
            let errors=self.peers.errors.lock().unwrap();assert!(errors.is_empty(),"fixture errors: {errors:?}");
        }
    }
    impl Drop for RuntimeFixture {fn drop(&mut self){self.finish();}}

    fn serve_peer(stream:std::net::TcpStream,control:PeerControl) {
        let mut record=None;
        let result=(||->Result<(),String> {
            stream.set_read_timeout(Some(Duration::from_millis(500))).map_err(|e|e.to_string())?;
            stream.set_write_timeout(Some(Duration::from_millis(500))).map_err(|e|e.to_string())?;
            let mut path=String::new();
            let mut socket=tungstenite::accept_hdr(stream,|request:&tungstenite::handshake::server::Request,response|{path=request.uri().path().into();Ok(response)}).map_err(|e|e.to_string())?;
            let request:Value=serde_json::from_str(&socket.read().map_err(|e|e.to_string())?.into_text().map_err(|e|e.to_string())?).map_err(|e|e.to_string())?;
            let expected=match path.as_str(){"/perp"=>json!(["ethusdt@bookTicker"]),"/spot"=>json!(["solusdt@bookTicker"]),_=>return Err("unknown fixture endpoint".into())};
            let original=request["params"]==json!(["btcusdt@bookTicker"]);
            if request["method"]!="SUBSCRIBE"||request["id"].as_u64().is_none()||(!original&&request["params"]!=expected){return Err(format!("unexpected SUBSCRIBE: {request}"))}
            let index={let mut records=control.records.lock().unwrap();let index=records.len();records.push(json!({"path":path,"request":request,"ack":false,"closed":false}));index};record=Some(index);
            socket.get_mut().set_read_timeout(Some(Duration::from_millis(10))).map_err(|e|e.to_string())?;
            let pre_ack=matches!(control.scenario,Scenario::PreAck)&&path=="/perp";
            let withheld=matches!(control.scenario,Scenario::ChangedTargets)&&path=="/spot"&&!original;
            if !pre_ack&&!withheld {send_ack(&mut socket,&request)?;control.records.lock().unwrap()[index]["ack"]=json!(true);}
            while !control.stop.load(Ordering::Acquire) {
                if pre_ack&&control.release.load(Ordering::Acquire) {
                    let quote=json!({"s":"BTCUSDT","u":1,"E":SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as u64,"b":"100","a":"101","B":"2","A":"3"});
                    socket.send(Message::Text(quote.to_string().into())).map_err(|e|e.to_string())?;
                    socket.close(None).map_err(|e|e.to_string())?;return Ok(())
                }
                if withheld&&control.release.load(Ordering::Acquire)&&control.records.lock().unwrap()[index]["ack"]==false {
                    send_ack(&mut socket,&request)?;control.records.lock().unwrap()[index]["ack"]=json!(true);
                }
                match socket.read() {
                    Err(tungstenite::Error::Io(e)) if matches!(e.kind(),std::io::ErrorKind::TimedOut|std::io::ErrorKind::WouldBlock)=>{},
                    Ok(Message::Close(_))=>{let _=socket.flush();break},
                    Err(tungstenite::Error::ConnectionClosed)|Err(tungstenite::Error::AlreadyClosed)=>break,
                    Err(e)=>{if !control.stop.load(Ordering::Acquire){return Err(e.to_string())}break},_=>{},
                }
            }
            Ok(())
        })();
        if let Some(index)=record {control.records.lock().unwrap()[index]["closed"]=json!(true);}
        if let Err(error)=result {control.errors.lock().unwrap().push(error);}
    }
    fn send_ack(socket:&mut tungstenite::WebSocket<std::net::TcpStream>,request:&Value)->Result<(),String> {
        socket.send(Message::Text(json!({"id":request["id"],"result":null}).to_string().into())).map_err(|e|e.to_string())
    }
    fn feed_rows(selector:&mut Selector,rows:Vec<Value>) {
        for row in rows {if row["type"]=="feed" {selector.apply(serde_json::from_value(row["action"].clone()).unwrap()).unwrap();}}
    }
    fn recv_until_acknowledged(runtime:&mut Runtime,count:usize,selector:&mut Selector) {
        let deadline=Instant::now()+Duration::from_secs(2);
        while runtime.accounting()["acknowledged"]!=count {
            assert!(Instant::now()<deadline,"ACK barrier: {}",runtime.accounting());
            feed_rows(selector,runtime.recv(Duration::from_millis(10)).unwrap());
        }
    }
    fn configured_selector(runtime:&Runtime)->Selector {
        let mut selector=Selector::default();selector.apply(Action::Configure{stream_id:"foundation-runtime".into(),mode:"single".into(),symbols:vec!["BTCUSDT".into()],clock:runtime.clock()}).unwrap();selector
    }
    fn phase(runtime:&Runtime)->Phase {
        let slot=runtime.control().handoff.unwrap();let status=slot.lock().unwrap().status();status.0.unwrap().phase
    }
    fn truth(runtime:&Runtime)->BTreeMap<String,Value> {runtime.control().physical.lock().unwrap().clone()}
    fn assert_truth(states:&BTreeMap<String,Value>,targets:&Targets) {
        assert_eq!(states.len(),2);
        for spot in [false,true] {
            let roles:Vec<_>=states.values().filter(|s|s["spot"]==spot).collect();assert_eq!(roles.len(),1);
            let state=roles[0];assert_eq!(state["connected"],true);assert_eq!(state["replacement"],false);
            assert!(state["token"].as_str().is_some_and(|token|token!="pending"&&!token.is_empty()));assert_eq!(state["generation"],1);
            assert_eq!(state["applied"],json!(if spot{&targets.spot}else{&targets.perpetual}));
        }
    }

    #[test]
    fn foundation_runtime_pre_ack_close_retains_first_failure_and_never_supplies() {
        let mut fixture=RuntimeFixture::start(Scenario::PreAck);
        let mut runtime=Runtime::start(fixture.settings.clone()).unwrap();
        let mut selector=configured_selector(&runtime);recv_until_acknowledged(&mut runtime,1,&mut selector);
        let subscribed=Instant::now()+Duration::from_secs(1);
        while fixture.records().len()<2 {assert!(Instant::now()<subscribed,"missing actual perpetual SUBSCRIBE");std::thread::sleep(Duration::from_millis(2));}
        assert_eq!(fixture.records().len(),2);assert!(fixture.records().iter().filter(|r|r["path"]=="/perp").all(|r|r["ack"]==false));
        fixture.peers.release.store(true,Ordering::Release);
        let deadline=Instant::now()+Duration::from_secs(2);
        let first=loop {
            assert!(Instant::now()<deadline,"pre-ACK owner failure was not observed");
            match runtime.recv(Duration::from_millis(10)){Err(error)=>break error,Ok(rows)=>{
                assert!(!rows.iter().any(|r|r["type"]=="spot_raw"||r["action"]["type"]=="raw_captured"));
                feed_rows(&mut selector,rows);
            }}
        };
        assert_eq!(first,PRE_ACK_FAILURE);
        // The first-failure check forbids a reconnect attempt, including after
        // the real maximum initial retry delay (1000 + 250 ms) has elapsed.
        let retry_observation=Instant::now()+Duration::from_millis(1300);
        while Instant::now()<retry_observation {
            assert_eq!(runtime.recv(Duration::ZERO).unwrap_err(),first);
            std::thread::sleep(Duration::from_millis(10));
        }
        let report=runtime.accounting();assert_eq!(report["first_failure"],first);
        assert_eq!(report["wire_raw_received"],1);assert_eq!(report["supplied"],0);assert_eq!(report["retired_frames"],0);
        assert_eq!(fixture.peers.accepted.load(Ordering::Acquire),2);
        runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
        assert_eq!(runtime.recv(Duration::ZERO).unwrap_err(),first);drop(runtime);
        fixture.finish();fixture.assert_clean(2);
    }

    fn await_changed_workers(runtime:&mut Runtime,selector:&mut Selector,fixture:&RuntimeFixture) {
        let deadline=Instant::now()+Duration::from_secs(2);
        loop {
            assert!(Instant::now()<deadline,"changed workers were not observed: {}",runtime.accounting());
            feed_rows(selector,runtime.progress_handoff().unwrap());
            feed_rows(selector,runtime.recv(Duration::from_millis(10)).unwrap());
            if fixture.records().len()==4&&runtime.accounting()["acknowledged"]==1 {break}
        }
    }
    fn assert_new_subscriptions(fixture:&RuntimeFixture) {
        let deadline=Instant::now()+Duration::from_secs(1);
        while fixture.records().iter().filter(|r|r["ack"]==true).count()<3 {assert!(Instant::now()<deadline,"actual ACK send records missing");std::thread::sleep(Duration::from_millis(2));}
        let records=fixture.records();assert_eq!(records.len(),4);
        for (path,param) in [("/perp","ethusdt@bookTicker"),("/spot","solusdt@bookTicker")] {
            let old:Vec<_>=records.iter().filter(|r|r["path"]==path&&r["request"]["params"]==json!(["btcusdt@bookTicker"])).collect();
            let new:Vec<_>=records.iter().filter(|r|r["path"]==path&&r["request"]["params"]==json!([param])).collect();
            assert_eq!(old.len(),1);assert_eq!(new.len(),1);assert_eq!(old[0]["ack"],true);
            assert_eq!(new[0]["ack"],path=="/perp");
        }
    }
    fn await_ready(runtime:&mut Runtime,selector:&mut Selector) {
        let deadline=Instant::now()+Duration::from_secs(2);
        while phase(runtime)!=Phase::Ready {
            assert!(Instant::now()<deadline,"handoff not ready: {}",runtime.accounting());
            feed_rows(selector,runtime.recv(Duration::from_millis(10)).unwrap());
            feed_rows(selector,runtime.progress_handoff().unwrap());
        }
    }
    fn finish_runtime(runtime:&mut Runtime) {
        runtime.request_handoff_shutdown().unwrap();let deadline=Instant::now()+Duration::from_secs(2);
        while !runtime.handoff_shutdown_ready().unwrap(){assert!(Instant::now()<deadline,"worker cleanup deadline");std::thread::sleep(Duration::from_millis(5));}
        runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);assert!(truth(runtime).is_empty());
    }

    #[test]
    fn foundation_runtime_changed_perpetual_and_spot_targets_require_actual_new_ack() {
        let mut fixture=RuntimeFixture::start(Scenario::ChangedTargets);
        let mut runtime=Runtime::start_configured(fixture.settings.clone()).unwrap();
        let mut selector=configured_selector(&runtime);recv_until_acknowledged(&mut runtime,2,&mut selector);
        let before=truth(&runtime);assert_truth(&before,&Targets{perpetual:vec!["BTCUSDT".into()],spot:vec!["BTCUSDT".into()]});
        let targets=Targets{perpetual:vec!["ETHUSDT".into()],spot:vec!["SOLUSDT".into()]};
        runtime.prepare_handoff(41,targets.clone()).unwrap();assert_eq!(phase(&runtime),Phase::Prepared);
        runtime.revalidate_handoff(41).unwrap();let slot=runtime.control().handoff.unwrap();
        slot.lock().unwrap().commit_logical(41,||selector.apply(Action::Targets{symbols:targets.perpetual.clone()}).map(|_|())).unwrap();
        assert_eq!(phase(&runtime),Phase::Committed);await_changed_workers(&mut runtime,&mut selector,&fixture);
        assert_new_subscriptions(&fixture);
        let withheld_until=Instant::now()+Duration::from_millis(80);
        while Instant::now()<withheld_until {
            feed_rows(&mut selector,runtime.progress_handoff().unwrap());
            feed_rows(&mut selector,runtime.recv(Duration::from_millis(10)).unwrap());
            assert_eq!(phase(&runtime),Phase::Applying);
        }
        assert_eq!(runtime.accounting()["configuration"]["active_workers"],2);
        assert_eq!(runtime.accounting()["configuration"]["retiring_workers"],0);
        fixture.peers.release.store(true,Ordering::Release);await_ready(&mut runtime,&mut selector);
        let after=truth(&runtime);assert_truth(&after,&targets);
        let old_ids:BTreeSet<_>=before.keys().collect();let new_ids:BTreeSet<_>=after.keys().collect();assert!(old_ids.is_disjoint(&new_ids));
        for old in before.values(){assert!(after.values().all(|new|new["token"]!=old["token"]));}
        {let mut slot=slot.lock().unwrap();slot.activate(41,&after).unwrap();assert_eq!(slot.status().0.unwrap().phase,Phase::Active);slot.finish_active().unwrap();
            let (current,last)=slot.status();assert!(current.is_none());let last=last.unwrap();assert_eq!(last.operation,41);assert_eq!(last.phase,Phase::Active);assert!(last.first_failure.is_none());}
        let closure_deadline=Instant::now()+Duration::from_secs(1);
        while fixture.records().iter().filter(|r|r["request"]["params"]==json!(["btcusdt@bookTicker"])).any(|r|r["closed"]!=true) {
            assert!(Instant::now()<closure_deadline,"old physical peers retained");std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(runtime.accounting()["acknowledged"],2);assert_eq!(runtime.accounting()["wire_raw_received"],0);assert_eq!(runtime.accounting()["supplied"],0);
        assert!(runtime.accounting()["first_failure"].is_null());finish_runtime(&mut runtime);drop(runtime);
        fixture.finish();fixture.assert_clean(4);
    }
}
