//! Bounded native-owned physical workers. Overflow invalidates the entire owner.
//! The consumer must retire its selector/Engine on any runtime error.
use crate::{Clock, Result, CAPACITY, MAX_SYMBOLS};
pub mod handoff;
use handoff::{Slot,Targets,Plan};
use crate::transport::{PublicSocket, Reception};
use serde::{Deserialize,Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex, Condvar, atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering}, mpsc::{self, Receiver, SyncSender}};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::io::Read;

#[derive(Clone,Copy)]
struct RecoveryPolicy {base_ms:u64,cap_ms:u64,stable_ms:u64,jitter_ms:u64}
impl Default for RecoveryPolicy {fn default()->Self{Self{base_ms:1000,cap_ms:30000,stable_ms:60000,jitter_ms:250}}}
impl RecoveryPolicy {fn spread(self)->u64{self.jitter_ms.min(self.cap_ms.saturating_sub(self.base_ms))}}
struct Recovery {policy:RecoveryPolicy,attempt:u8,opened_ms:Option<u64>}
impl Recovery {
    fn new(policy:RecoveryPolicy)->Self{Self{policy,attempt:0,opened_ms:None}}
    fn opened(&mut self,now_ms:u64){self.opened_ms=Some(now_ms);}
    fn retry_delay(&mut self,now_ms:u64,jitter:u64)->u64{
        let age=self.opened_ms.take().map_or(0,|opened|now_ms.saturating_sub(opened));
        if age>=self.policy.stable_ms{self.attempt=0;}
        self.attempt=(self.attempt+1).min(6);
        let delay=self.policy.base_ms.saturating_mul(1_u64<<(self.attempt-1)).min(self.policy.cap_ms);
        delay.min(self.policy.cap_ms-self.policy.spread())+jitter
    }
}
#[derive(Clone,Copy)]
struct RotationPolicy {rotation_ms:u64,stagger_ms:u64,timeout_ms:u64,retry_ms:u64}
impl Default for RotationPolicy {fn default()->Self{Self{rotation_ms:79200000,stagger_ms:3600000,timeout_ms:30000,retry_ms:60000}}}
impl RotationPolicy {
    fn initial_due(self,now:u64,slot:u8)->u64{now+self.rotation_ms+if slot==1{self.stagger_ms}else{0}}
    fn promoted_due(self,now:u64)->u64{now+self.rotation_ms}
    fn retry_due(self,now:u64)->u64{now+self.retry_ms}
    fn expired(self,created:u64,now:u64)->bool{now.saturating_sub(created)>=self.timeout_ms}
    fn due(self,due:u64,now:u64)->bool{now>=due}
}
#[derive(Clone,Copy,Default)]
struct LifecyclePolicy {recovery:RecoveryPolicy,rotation:RotationPolicy}
fn public_jitter(spread:u64)->Result<u64>{
    if spread==0{return Ok(0)}
    // Retry-only entropy, never on the quote path. Rejection sampling preserves
    // the inclusive uniform 0..250 public jitter without a new dependency.
    let mut source=std::fs::File::open("/dev/urandom").map_err(|_|"public reconnect entropy unavailable")?;
    let width=spread+1;let accepted=256-(256%width);
    for _ in 0..16 {let mut byte=[0];source.read_exact(&mut byte).map_err(|_|"public reconnect entropy failed")?;if u64::from(byte[0])<accepted{return Ok(u64::from(byte[0])%width)}}
    Err("public reconnect entropy exhausted".into())
}
fn retry_wait(stop:&AtomicBool,failure:&Mutex<Option<String>>,delay_ms:u64)->bool{
    let until=Instant::now()+Duration::from_millis(delay_ms);
    loop{if stop.load(Ordering::Acquire)||failure.lock().map_or(true,|f|f.is_some()){return false}
        let remaining=until.saturating_duration_since(Instant::now());if remaining.is_zero(){return true}thread::sleep(remaining.min(Duration::from_millis(10)));
    }
}

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Endpoint { pub address: SocketAddr, pub uri: String }
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settings {
    pub session: String, pub mode: String, pub perpetual: Endpoint, pub spot: Endpoint,
    pub perpetual_symbols: Vec<String>, pub spot_symbols: Vec<String>,
    #[serde(default)] pub fixture_wall_ms:Option<i64>,
    #[serde(default)] pub fixture_backup_endpoint:Option<Endpoint>,
}
enum Event {
    Connected { route:String, token:String, generation:u64 },
    Frame { route:String, token:String, generation:u64, payload:Value, clock:Clock },
    Disconnected { route:String, generation:u64 },
}
struct Worker { stop:Arc<AtomicBool>, join:Option<JoinHandle<()>>, slot:u8, spot:bool,
    replacement:bool, token:String, generation:u64, connected:bool, created_ms:u64,due_ms:u64 }
fn promotion_current(worker:&Worker,state:&Value,targets:&[String])->bool{
    let expected:BTreeSet<_>=targets.iter().map(String::as_str).collect();
    let applied=state["applied"].as_array().and_then(|symbols|symbols.iter().map(Value::as_str).collect::<Option<BTreeSet<_>>>());
    worker.connected&&state["connected"]==true&&state["token"]==worker.token&&state["generation"]==worker.generation&&state["replacement"]==true&&applied.as_ref()==Some(&expected)
}
fn commit_promotion_stop(_physical_guard:&std::sync::MutexGuard<'_,BTreeMap<String,Value>>,stop:&AtomicBool) {
    #[cfg(test)]
    let probe=foundation_promotion_tests::begin_stop_commit(_physical_guard,stop);
    stop.store(true,Ordering::Release);
    #[cfg(test)]
    foundation_promotion_tests::finish_stop_commit(_physical_guard,stop,probe);
}
/// Cheap read-boundary counters. Histograms/cleanup are intentionally absent.
#[derive(Clone,Copy,Serialize)]
pub struct FailureProgress {wire_raw_received:u64,supplied:u64,retired_frames:u64,active_workers:usize,retiring_workers:usize}
/// One retained notification, independent of receiver thread identity. No payload
/// or growing history lives here; the existing channels retain all ownership.
#[derive(Clone,Default)]
pub struct WakeSignal { state:Arc<(Mutex<bool>,Condvar)> }
impl WakeSignal {
    pub fn notify(&self) {
        let (pending,ready)=&*self.state;
        {let mut pending=pending.lock().unwrap_or_else(|e|e.into_inner());*pending=true;}
        ready.notify_one();
    }
    pub fn wait(&self,timeout:Duration)->Result<()> {
        let pending=self.state.0.lock().map_err(|_|"native wake lock poisoned")?;
        self.wait_pending(pending,timeout)
    }
    fn wait_pending(&self,mut pending:std::sync::MutexGuard<'_,bool>,timeout:Duration)->Result<()> {
        let ready=&self.state.1;
        // A notification before this wait is retained. Condvar atomically
        // releases the mutex on wait, closing the check-to-park race.
        if !*pending {pending=ready.wait_timeout(pending,timeout).map_err(|_|"native wake lock poisoned")?.0;}
        *pending=false;Ok(())
    }
}
#[derive(Clone)]
pub struct Control {
    pub wake:WakeSignal,
    pub physical:Arc<Mutex<BTreeMap<String,Value>>>,
    pub promotion:Arc<Mutex<Option<String>>>,
    pub origin:Instant, pub fixture_wall:Arc<AtomicI64>,
    pub failure:Arc<Mutex<Option<String>>>,
    pub failure_accounting:Arc<Mutex<Option<Value>>>,
    pub handoff:Option<Arc<Mutex<Slot>>>,
    pub failure_progress:Arc<Mutex<Option<FailureProgress>>>,
}
impl Default for Control {fn default()->Self{Self{wake:WakeSignal::default(),physical:Arc::new(Mutex::new(BTreeMap::new())),promotion:Arc::new(Mutex::new(None)),origin:Instant::now(),fixture_wall:Arc::new(AtomicI64::new(0)),failure:Arc::new(Mutex::new(None)),failure_accounting:Arc::new(Mutex::new(None)),handoff:None,failure_progress:Arc::new(Mutex::new(None))}}}
impl Control {pub fn clock(&self)->Clock {let mut c=clock(self.origin);let wall=self.fixture_wall.load(Ordering::Acquire);if wall!=0{c.wall_ms=wall+c.monotonic_ms;}c}}
pub struct Runtime {
    settings:Settings, workers:BTreeMap<String,Worker>, sender:SyncSender<Event>, receiver:Receiver<Event>,
    failure:Arc<Mutex<Option<String>>>, origin:Instant, next:u64, generations:BTreeMap<String,u64>,
    memberships:BTreeMap<String,u64>, serials:BTreeMap<String,u64>, supplied:u64, retired:u64,
    fixture_wall:Arc<AtomicI64>, anchored:bool,
    received:Arc<AtomicU64>, retired_frames:u64, queue_age:AgeStats, source_age:AgeStats,
    control:Control,
    recv_total:Timing, channel_wait:Timing,
    lifecycle:LifecyclePolicy,
    retiring:BTreeMap<String,Worker>, pending_targets:Option<Arc<Plan>>,
}
#[derive(Default,Serialize)]
struct Timing {calls:u64,total_ns:u128,max_ns:u128}
impl Timing {fn record(&mut self,start:Instant){let ns=start.elapsed().as_nanos();self.calls+=1;self.total_ns+=ns;self.max_ns=self.max_ns.max(ns);}}
struct AgeStats {bins:[u64;1002],count:u64,max:i64}
impl Default for AgeStats {fn default()->Self{Self{bins:[0;1002],count:0,max:0}}}
impl AgeStats {
    fn record(&mut self,age:i64){self.count+=1;self.max=self.max.max(age);self.bins[age.clamp(0,1001) as usize]+=1;}
    fn value(&self)->Value{let percentile=|p:u64|{let target=(self.count*p).div_ceil(100);let mut seen=0;for (index,count) in self.bins.iter().enumerate(){seen+=count;if seen>=target&&target>0{return if index<=1000{json!(index)}else{Value::Null}}}Value::Null};json!({"count":self.count,"max_ms":self.max,"p50_ms":percentile(50),"p95_ms":percentile(95),"p99_ms":percentile(99),"over1000ms":self.bins[1001]})}
}
fn fail(failure:&Mutex<Option<String>>, reason:String) { if let Ok(mut value)=failure.lock(){if value.is_none(){*value=Some(reason)}} }
// Private generic ownership seam permits RAII tests without changing Event or
// adding quote metadata. Production instantiates this only for the actual Event.
fn emit<T>(sender:&SyncSender<T>, failure:&Mutex<Option<String>>, event:T)->bool {
    match sender.try_send(event) {
        Ok(())=>true,
        Err(mpsc::TrySendError::Full(returned))=>{
            drop(returned); // Preserve release-before-failure-latch ordering.
            fail(failure,"native ingress full; owner incomplete".into());false
        },
        Err(mpsc::TrySendError::Disconnected(returned))=>{
            drop(returned);
            fail(failure,"native ingress receiver disconnected; owner incomplete".into());false
        },
    }
}
fn fail_notified(failure:&Mutex<Option<String>>,wake:&WakeSignal,reason:String) {
    fail(failure,reason);wake.notify();
}
fn emit_notified<T>(sender:&SyncSender<T>,failure:&Mutex<Option<String>>,wake:&WakeSignal,event:T)->bool {
    // emit retains the original exact payload release / first-error ordering.
    let sent=emit(sender,failure,event);wake.notify();sent
}
// Timestamp compatibility only: CPython _PyTime_AsSecondsDouble converts the
// total signed-i64 nanoseconds before dividing, except at exact whole seconds.
// A Duration outside that reference domain cannot be a supported native clock.
fn cpython_wall_seconds(duration:Duration)->Option<f64> {
    let nanos=i64::try_from(duration.as_nanos()).ok()?;
    Some(if duration.subsec_nanos()==0{duration.as_secs() as f64}else{nanos as f64/1_000_000_000.0})
}
fn wall_ms_from_duration(duration:Duration)->i64 {
    // The conversion domain is at most 9.23e12 ms, safely inside i64. Zero is an
    // invalid Clock wall, preserving pre-Epoch failure without narrowing wraps.
    cpython_wall_seconds(duration).map_or(0,|seconds|(seconds*1000.0).round_ties_even() as i64)
}
fn clock(origin:Instant)->Clock { Clock {wall_ms:wall_ms_from_duration(SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default()),monotonic_ms:origin.elapsed().as_millis() as i64} }
impl Runtime {
    pub fn start(settings:Settings)->Result<Self> {
        Self::start_with_policy(settings,LifecyclePolicy::default())
    }
    pub fn start_configured(settings:Settings)->Result<Self>{Self::start_with_policy_and_handoff(settings,LifecyclePolicy::default(),true)}
    fn start_with_policy(settings:Settings,lifecycle:LifecyclePolicy)->Result<Self>{Self::start_with_policy_and_handoff(settings,lifecycle,false)}
    fn start_with_policy_and_handoff(settings:Settings,lifecycle:LifecyclePolicy,configured:bool)->Result<Self>{
        if settings.session.is_empty()||settings.session.len()>40||!settings.session.bytes().all(|c|c.is_ascii_alphanumeric()||c==b'-')||!matches!(settings.mode.as_str(),"single"|"shadow"|"redundant") {return Err("invalid native session/mode".into())}
        PublicSocket::validate_endpoint(settings.perpetual.address, &settings.perpetual.uri)?;
        let starts_spot = !configured || !settings.spot_symbols.is_empty();
        if starts_spot {
            PublicSocket::validate_endpoint(settings.spot.address, &settings.spot.uri)?;
        }
        if settings.mode != "single" {
            if let Some(backup) = settings.fixture_backup_endpoint.as_ref() {
                PublicSocket::validate_endpoint(backup.address, &backup.uri)?;
            }
        }
        if settings.fixture_wall_ms.is_some()&&(!settings.perpetual.address.ip().is_loopback()||!settings.spot.address.ip().is_loopback()){return Err("fixture clock requires loopback endpoints".into())}
        if settings.fixture_backup_endpoint.as_ref().is_some_and(|e|!e.address.ip().is_loopback()||!settings.perpetual.address.ip().is_loopback()||!settings.spot.address.ip().is_loopback()){return Err("fixture backup requires loopback endpoints".into())}
        for (spot,symbols) in [(false,&settings.perpetual_symbols),(true,&settings.spot_symbols)] {
            if (symbols.is_empty()&&!(configured&&spot))||symbols.len()>MAX_SYMBOLS||symbols.iter().any(|s|!crate::symbol_valid(s))||symbols.iter().collect::<BTreeSet<_>>().len()!=symbols.len(){return Err("invalid native subscriptions".into())}
        }
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let memberships=settings.perpetual_symbols.iter().map(|s|(s.clone(),1)).collect();
        let fixture_wall=Arc::new(AtomicI64::new(settings.fixture_wall_ms.unwrap_or(0)));
        let mut control=Control::default();control.fixture_wall=fixture_wall.clone();
        let mut this=Self {settings,workers:BTreeMap::new(),sender,receiver,failure:control.failure.clone(),origin:control.origin,next:0,generations:BTreeMap::new(),memberships,serials:BTreeMap::new(),supplied:0,retired:0,fixture_wall,anchored:false,received:Arc::new(AtomicU64::new(0)),retired_frames:0,queue_age:AgeStats::default(),source_age:AgeStats::default(),control,recv_total:Timing::default(),channel_wait:Timing::default(),lifecycle,retiring:BTreeMap::new(),pending_targets:None};
        this.spawn(0,false,false)?;
        if this.settings.mode!="single" {this.spawn(1,false,false)?;}
        if configured{this.enable_handoff()?;}
        if starts_spot{this.spawn(0,true,false)?;}Ok(this)
    }
    fn spawn(&mut self,slot:u8,spot:bool,replacement:bool)->Result<()> {
        if self.workers.values().chain(self.retiring.values()).filter(|w|w.spot==spot).count()>=if spot{1}else{3}{return Err("native physical slot capacity".into())}
        self.next+=1;let route=format!("{}-{}-{}",if spot{"spot"}else{"perp"},slot,self.next);
        let created_ms=self.origin.elapsed().as_millis() as u64;let due_ms=self.lifecycle.rotation.initial_due(created_ms,slot);
        let endpoint=if spot{self.settings.spot.clone()}else if slot==1{self.settings.fixture_backup_endpoint.clone().unwrap_or_else(||self.settings.perpetual.clone())}else{self.settings.perpetual.clone()};
        let symbols=if spot{self.settings.spot_symbols.clone()}else{self.settings.perpetual_symbols.clone()};
        let stop=Arc::new(AtomicBool::new(false));let worker_stop=stop.clone();let failure=self.failure.clone();let sender=self.sender.clone();
        let origin=self.origin;let fixture_wall=self.fixture_wall.clone();let received=self.received.clone();let id=route.clone();let session=self.settings.session.clone();
        let physical=self.control.physical.clone();let wake=self.control.wake.clone();
        physical.lock().map_err(|_|"physical truth poisoned")?.insert(route.clone(),json!({"type":"route","route":route,"slot":slot,"spot":spot,"token":"pending","generation":1,"connected":false,"replacement":replacement,"applied":[]}));
        let recovery=self.lifecycle.recovery;
        let join=thread::Builder::new().name(route.clone()).spawn(move||{
            let mut generation=0;let mut recovery=Recovery::new(recovery);
            while !worker_stop.load(Ordering::Acquire) {
                if failure.lock().map_or(true,|f|f.is_some()){break}
                generation+=1;let token=format!("{session}-{id}-{generation}");
                let mut unqualified=0usize;
                let attempt=(||->Result<()> {
                    let mut socket=PublicSocket::connect(endpoint.address,&endpoint.uri)?;
                    recovery.opened(origin.elapsed().as_millis() as u64);
                    socket.subscribe(generation,&symbols)?;
                    let ack_deadline=Instant::now()+Duration::from_secs(5);
                    let mut ack=false;let mut before_ack=Vec::new();
                    while !worker_stop.load(Ordering::Acquire) {
                        if !ack&&Instant::now()>ack_deadline{return Err("subscription ACK deadline".into())}
                        match socket.receive_timeout()? {
                            Reception::Idle=>continue,
                            Reception::Event(event)=>match event["type"].as_str() {
                                Some("ack")=>{
                                    ack=true;
                                    {let mut states=physical.lock().map_err(|_|"physical truth poisoned")?;let state=states.get_mut(&id).ok_or("retired physical owner")?;state["token"]=json!(token);state["generation"]=json!(generation);state["connected"]=json!(true);state["applied"]=json!(symbols);}
                                    if !emit_notified(&sender,&failure,&wake,Event::Connected{route:id.clone(),token:token.clone(),generation}){return Err("ingress failed".into())}
                                    for (payload,clock) in before_ack.drain(..) {if !emit_notified(&sender,&failure,&wake,Event::Frame{route:id.clone(),token:token.clone(),generation,payload,clock}){return Err("ingress failed".into())}unqualified-=1;}
                                },
                                Some("raw")=>{
                                    received.fetch_add(1,Ordering::Relaxed);
                                    let payload=event["payload"].clone();let mut clock=clock(origin);let wall=fixture_wall.load(Ordering::Acquire);if wall!=0{clock.wall_ms=wall+clock.monotonic_ms;}
                                    if !ack {if before_ack.len()>=CAPACITY{return Err("pre-ACK buffer full".into())}before_ack.push((payload,clock));unqualified+=1;}
                                    else if !emit_notified(&sender,&failure,&wake,Event::Frame{route:id.clone(),token:token.clone(),generation,payload,clock}) {return Err("ingress failed".into())}
                                },
                                Some("ping"|"pong")=>{},_=>return Err("unknown physical frame".into()),
                            }
                        }
                    }
                    let _=socket.close();Ok(())
                })();
                if let Ok(mut states)=physical.lock(){if let Some(state)=states.get_mut(&id){state["connected"]=json!(false);state["applied"]=json!([]);}}
                if unqualified>0 {fail_notified(&failure,&wake,format!("retired {unqualified} pre-ACK frames; owner incomplete"));}
                if !emit_notified(&sender,&failure,&wake,Event::Disconnected{route:id.clone(),generation}) {break}
                if worker_stop.load(Ordering::Acquire)||failure.lock().map_or(true,|f|f.is_some()){break}
                // Subscription or parse uncertainty cannot resume the same physical generation.
                let _=attempt;
                let jitter=match public_jitter(recovery.policy.spread()){Ok(jitter)=>jitter,Err(error)=>{fail_notified(&failure,&wake,error);break}};
                let delay=recovery.retry_delay(origin.elapsed().as_millis() as u64,jitter);
                if !retry_wait(&worker_stop,&failure,delay){break}
            }
        }).map_err(|e|e.to_string())?;
        self.serials.insert(route.clone(),0);
        self.workers.insert(route,Worker {stop,join:Some(join),slot,spot,replacement,token:String::new(),generation:0,connected:false,created_ms,due_ms});Ok(())
    }
    /// Internal Rust opt-in; existing live headers never call this method.
    pub fn enable_handoff(&mut self)->Result<()> {
        if self.control.handoff.is_some(){return Err("configuration handoff already enabled".into())}
        self.control.handoff=Some(Arc::new(Mutex::new(Slot::default())));Ok(())
    }
    fn target_state(&self)->Targets {Targets{perpetual:self.settings.perpetual_symbols.clone(),spot:self.settings.spot_symbols.clone()}.normalize()}
    pub fn handoff_enabled(&self)->bool{self.control.handoff.is_some()}
    fn check_handoff(&self,targets:&Targets)->Result<()> {
        if self.pending_targets.is_some()||!self.retiring.is_empty()||self.workers.values().any(|w|w.replacement){return Err("configuration physical operation busy".into())}
        for symbols in [&targets.perpetual,&targets.spot] {
            if symbols.len()>MAX_SYMBOLS||symbols.iter().any(|s|!crate::symbol_valid(s))||symbols.iter().collect::<BTreeSet<_>>().len()!=symbols.len(){return Err("invalid configuration targets".into())}
        }
        if targets.perpetual.is_empty(){return Err("empty configuration perpetual targets".into())}
        let prospective:BTreeSet<_>=self.memberships.keys().chain(targets.perpetual.iter()).collect();
        if prospective.len()>MAX_SYMBOLS{return Err("lifetime subscription capacity".into())}
        Ok(())
    }
    pub fn prepare_handoff(&mut self,operation:u64,targets:Targets)->Result<()> {
        let targets=targets.normalize();self.check_handoff(&targets)?;
        let slot=self.control.handoff.as_ref().ok_or("configuration handoff disabled")?;
        slot.lock().map_err(|_|"configuration slot poisoned")?.prepare(Plan{operation,before:self.target_state(),after:targets})
    }
    pub fn revalidate_handoff(&self,operation:u64)->Result<()> {
        let slot=self.control.handoff.as_ref().ok_or("configuration handoff disabled")?.lock().map_err(|_|"configuration slot poisoned")?;
        let targets=slot.targets(operation)?;slot.revalidate(&self.target_state())?;self.check_handoff(&targets)
    }
    fn handoff_fenced(&self)->bool {self.control.handoff.as_ref().is_some_and(|s|s.lock().map_or(true,|s|s.fenced()))}
    fn detach_handoff(&mut self,routes:Vec<String>)->Result<Vec<Value>> {
        let mut rows=Vec::new();
        // Same mutex as ACK callbacks: an old worker can no longer publish truth.
        let mut truth=self.control.physical.lock().map_err(|_|"physical truth poisoned")?;
        for route in routes {
            let Some(worker)=self.workers.remove(&route) else{continue};
            worker.stop.store(true,Ordering::Release);truth.remove(&route);
            if worker.spot {if worker.generation>0&&!worker.token.is_empty(){rows.push(json!({"type":"spot_route","token":worker.token,"generation":worker.generation,"connected":false,"symbols":self.settings.spot_symbols}));}}
            else if worker.generation>0 {rows.push(json!({"type":"feed","action":{"type":"retire","route":route}}));}
            self.generations.remove(&route);self.serials.remove(&route);self.retired+=1;
            self.retiring.insert(route,worker);
        }
        Ok(rows)
    }
    fn reap_ready(&mut self)->Result<()> {
        let ready:Vec<_>=self.retiring.iter().filter(|(_,w)|w.join.as_ref().is_none_or(JoinHandle::is_finished)).map(|(r,_)|r.clone()).collect();
        for route in ready {
            let mut worker=self.retiring.remove(&route).unwrap();
            if let Some(join)=worker.join.take(){join.join().map_err(|_|"native retiring worker panicked")?;}
        }
        Ok(())
    }
    fn progress_handoff_inner(&mut self)->Result<Vec<Value>> {
        let Some(slot)=self.control.handoff.clone() else{return Ok(vec![])};
        let command={let mut slot=slot.lock().map_err(|_|"configuration slot poisoned")?;if let Some(error)=slot.first_failure(){return Err(error)}if !matches!(slot.phase(),Some(handoff::Phase::Committed|handoff::Phase::Applying|handoff::Phase::Ready)){return Ok(vec![])}slot.command()};
        let mut rows=Vec::new();
        if let Some(plan)=command {
            if plan.before!=self.target_state(){return Err("configuration physical baseline changed".into())}
            self.check_handoff(&plan.after)?;
            let perp=plan.before.perpetual!=plan.after.perpetual;let spot=plan.before.spot!=plan.after.spot;
            let routes=self.workers.iter().filter(|(_,w)|if w.spot{spot}else{perp}).map(|(r,_)|r.clone()).collect();
            rows.extend(self.detach_handoff(routes)?);
            if perp {
                for s in plan.before.perpetual.iter().chain(&plan.after.perpetual){if plan.before.perpetual.contains(s)!=plan.after.perpetual.contains(s){*self.memberships.entry(s.clone()).or_default()+=1;}}
                rows.push(json!({"type":"feed","action":{"type":"targets","symbols":plan.after.perpetual}}));
            }
            if perp{self.settings.perpetual_symbols=plan.after.perpetual.clone();}if spot{self.settings.spot_symbols=plan.after.spot.clone();}self.pending_targets=Some(plan);
        }
        self.reap_ready()?;
        if self.retiring.is_empty() {
            if let Some(plan)=self.pending_targets.take() {
                if plan.before.perpetual!=plan.after.perpetual {self.spawn(0,false,false)?;if self.settings.mode!="single"{self.spawn(1,false,false)?;}}
                if plan.before.spot!=plan.after.spot&&!plan.after.spot.is_empty(){self.spawn(0,true,false)?;}
            }
            let truth=self.control.physical.lock().map_err(|_|"physical truth poisoned")?;
            let required_perpetual=if self.settings.mode=="single"{1}else{2};
            let qualified=self.workers.values().filter(|w|!w.spot).count()==required_perpetual&&self.workers.values().filter(|w|w.spot).count()==usize::from(!self.settings.spot_symbols.is_empty())&&self.workers.iter().all(|(route,w)| {
                let targets=if w.spot{&self.settings.spot_symbols}else{&self.settings.perpetual_symbols};
                truth.get(route).is_some_and(|s|w.connected&&s["connected"]==true&&s["token"]==w.token&&s["generation"]==w.generation&&s["applied"].as_array().and_then(|a|a.iter().map(Value::as_str).collect::<Option<BTreeSet<_>>>())==Some(targets.iter().map(String::as_str).collect()))
            });
            if qualified {slot.lock().map_err(|_|"configuration slot poisoned")?.ready(&truth);}else{slot.lock().map_err(|_|"configuration slot poisoned")?.not_ready();}
        }
        Ok(rows)
    }
    fn latch_handoff_failure(&self,error:String)->String {
        fail_notified(&self.failure,&self.control.wake,error.clone());
        let first=self.failure.lock().ok().and_then(|f|f.clone()).unwrap_or(error);
        if let Some(slot)=&self.control.handoff{if let Ok(mut slot)=slot.lock(){slot.fail(first.clone());}}
        first
    }
    /// One bounded read-boundary step; never waits for actor feedback or a thread.
    pub fn progress_handoff(&mut self)->Result<Vec<Value>> {
        let prior=self.failure.lock().map_err(|_|"native failure lock poisoned")?.clone();
        if let Some(error)=prior{return Err(self.latch_handoff_failure(error))}
        self.progress_handoff_inner().map_err(|error|self.latch_handoff_failure(error))
    }
    pub fn request_handoff_shutdown(&mut self)->Result<Vec<Value>> {
        if !self.handoff_enabled(){return Err("configuration handoff disabled".into())}
        self.pending_targets=None;let routes=self.workers.keys().cloned().collect();self.detach_handoff(routes)
    }
    pub fn handoff_shutdown_ready(&mut self)->Result<bool> {
        let prior=self.failure.lock().map_err(|_|"native failure lock poisoned")?.clone();if let Some(error)=prior{return Err(self.latch_handoff_failure(error))}
        self.reap_ready().map_err(|e|self.latch_handoff_failure(e))?;Ok(self.retiring.is_empty())
    }
    pub fn failure_progress(&self)->FailureProgress {FailureProgress{wire_raw_received:self.received.load(Ordering::Acquire),supplied:self.supplied,retired_frames:self.retired_frames,active_workers:self.workers.len(),retiring_workers:self.retiring.len()}}
    pub fn handoff_status(&self)->Option<Value> {
        self.control.handoff.as_ref().map(|slot|match slot.lock(){Ok(slot)=>json!({"receipts":slot.status(),"active_workers":self.workers.len(),"retiring_workers":self.retiring.len()}),Err(_)=>json!({"failed":"configuration slot poisoned"})})
    }
    pub fn control(&self)->Control {self.control.clone()}
    pub fn clock(&self)->Clock {let mut c=clock(self.origin);let wall=self.fixture_wall.load(Ordering::Acquire);if wall!=0{c.wall_ms=wall+c.monotonic_ms;}c}
    pub fn anchor_fixture(&mut self,wall_ms:i64)->Result<Value>{
        if self.settings.fixture_wall_ms.is_none()||self.anchored||self.supplied!=0||self.received.load(Ordering::Acquire)!=0||wall_ms<=0||self.workers.values().any(|w|!w.connected)||!matches!(self.receiver.try_recv(),Err(mpsc::TryRecvError::Empty)){return Err("fixture anchor requires once-only ACK-qualified empty pre-raw owner".into())}
        let mono=self.origin.elapsed().as_millis() as i64;
        self.fixture_wall.store(wall_ms-mono,Ordering::Release);self.anchored=true;
        Ok(json!({"wall_ms":wall_ms,"monotonic_ms":mono,"workers":self.workers.len()}))
    }
    pub fn rotate(&mut self,slot:u8)->Result<()> {
        if self.handoff_fenced(){return Err("configuration physical operation busy".into())}
        if slot>1||self.workers.values().any(|w|w.replacement)||!self.workers.values().any(|w|!w.spot&&w.slot==slot&&!w.replacement){return Err("invalid or concurrent rotation".into())}
        self.spawn(slot,false,true)
    }
    /// Subscription changes retire all old subscription truth before reconnect.
    /// Perpetual make-before-break is reserved for rotations with identical targets.
    pub fn targets(&mut self,perpetual:Vec<String>,spot:Vec<String>)->Result<Vec<Value>> {
        if self.handoff_fenced(){return Err("configuration physical operation busy".into())}
        for symbols in [&perpetual,&spot]{if symbols.is_empty()||symbols.len()>MAX_SYMBOLS||symbols.iter().any(|s|!crate::symbol_valid(s))||symbols.iter().collect::<BTreeSet<_>>().len()!=symbols.len(){return Err("invalid target change".into())}}
        for s in self.settings.perpetual_symbols.iter().chain(&perpetual) {if self.settings.perpetual_symbols.contains(s)!=perpetual.contains(s){*self.memberships.entry(s.clone()).or_default()+=1;}}
        if self.memberships.len()>MAX_SYMBOLS{return Err("lifetime subscription capacity".into())}
        let mut rows=Vec::new();for route in self.workers.keys().cloned().collect::<Vec<_>>() {rows.extend(self.retire(&route)?);}
        self.settings.perpetual_symbols=perpetual.clone();self.settings.spot_symbols=spot;
        rows.push(json!({"type":"feed","action":{"type":"targets","symbols":perpetual}}));
        self.spawn(0,false,false)?;if self.settings.mode!="single"{self.spawn(1,false,false)?;}self.spawn(0,true,false)?;Ok(rows)
    }
    fn retire(&mut self,route:&str)->Result<Vec<Value>> {
        let Some(mut worker)=self.workers.remove(route) else{return Ok(vec![])};
        worker.stop.store(true,Ordering::Release);
        if let Some(join)=worker.join.take(){join.join().map_err(|_|"native worker panicked")?;}
        self.control.physical.lock().map_err(|_|"physical truth poisoned")?.remove(route);
        self.retired+=1;self.generations.remove(route);self.serials.remove(route);
        Ok(if worker.spot {vec![json!({"type":"spot_route","token":worker.token,"generation":worker.generation.max(1),"connected":false,"symbols":self.settings.spot_symbols})]}else if worker.generation>0 {vec![json!({"type":"feed","action":{"type":"retire","route":route}})]}else{vec![]})
    }
    pub fn recv(&mut self,timeout:Duration)->Result<Vec<Value>> {
        let start=Instant::now();let result=self.recv_inner(timeout);self.recv_total.record(start);result
    }
    fn recv_inner(&mut self,timeout:Duration)->Result<Vec<Value>> {
        if let Some(error)=self.failure.lock().map_err(|_|"native failure lock poisoned")?.clone(){return Err(error)}
        let now=self.origin.elapsed().as_millis() as u64;
        if !self.handoff_fenced(){
        let promotion=self.control.promotion.lock().map_err(|_|"promotion truth poisoned")?.take();
        if let Some(route)=promotion {
            if let Some(worker)=self.workers.get(&route) {
                // Selector feedback alone is insufficient if a worker has
                // already invalidated shared truth before its queued event.
                let qualified={let truth=self.control.physical.lock().map_err(|_|"physical truth poisoned")?;truth.get(&route).is_some_and(|state|promotion_current(worker,state,&self.settings.perpetual_symbols))};
                if worker.replacement&&qualified {
                    #[cfg(test)]
                    foundation_promotion_tests::pause_after_qualified(&self.control,&route);
                    let slot=worker.slot;let old=self.workers.iter().find(|(id,w)|*id!=&route&&!w.spot&&w.slot==slot&&!w.replacement).map(|(id,_)|id.clone());
                    {
                        // Serialize qualification and the stop commit with
                        // worker truth updates; retirement joins only after unlock.
                        let truth=self.control.physical.lock().map_err(|_|"physical truth poisoned")?;
                        if !truth.get(&route).is_some_and(|state|promotion_current(worker,state,&self.settings.perpetual_symbols)){return Ok(vec![])}
                        if let Some(old)=old.as_ref().and_then(|route|self.workers.get(route)){commit_promotion_stop(&truth,old.stop.as_ref());}
                    }
                    let mut rows=Vec::new();if let Some(old)=old{rows.extend(self.retire(&old)?);}
                    let worker=self.workers.get_mut(&route).unwrap();worker.replacement=false;worker.due_ms=self.lifecycle.rotation.promoted_due(now);
                    self.control.physical.lock().map_err(|_|"physical truth poisoned")?.get_mut(&route).ok_or("missing physical replacement")?["replacement"]=json!(false);
                    rows.push(json!({"type":"feed","action":{"type":"route","route":route,"slot":slot,"token":worker.token,"generation":worker.generation,"connected":worker.connected,"replacement":false,"applied":self.settings.perpetual_symbols}}));return Ok(rows)
                }
            }
        }
        if let Some((route,slot))=self.workers.iter().find(|(_,w)|w.replacement&&self.lifecycle.rotation.expired(w.created_ms,now)).map(|(id,w)|(id.clone(),w.slot)){
            let rows=self.retire(&route)?;if let Some(old)=self.workers.values_mut().find(|w|!w.spot&&!w.replacement&&w.slot==slot){old.due_ms=self.lifecycle.rotation.retry_due(now);}return Ok(rows)
        }
        if self.settings.mode!="single"&&!self.settings.perpetual_symbols.is_empty()&&!self.workers.values().any(|w|w.replacement){
            if let Some(slot)=self.workers.values().filter(|w|!w.spot&&!w.replacement&&self.lifecycle.rotation.due(w.due_ms,now)).min_by_key(|w|w.due_ms).map(|w|w.slot){self.rotate(slot)?;}
        }
        }
        let waiting=Instant::now();let received=self.receive_event(timeout);self.channel_wait.record(waiting);
        let Some(event)=received? else{return Ok(vec![])};
        let route=match &event{Event::Connected{route,..}|Event::Frame{route,..}|Event::Disconnected{route,..}=>route};
        if !self.workers.contains_key(route){self.retired+=1;if matches!(event,Event::Frame{..}){self.retired_frames+=1;}return Ok(vec![])}
        let mut rows=Vec::new();
        match event {
            Event::Connected{route,token,generation}=>{
                let worker=self.workers.get_mut(&route).unwrap();worker.token=token.clone();worker.generation=generation;worker.connected=true;self.generations.insert(route.clone(),generation);
                if worker.spot{rows.push(json!({"type":"spot_route","token":token,"generation":generation,"connected":true,"symbols":self.settings.spot_symbols}));}
                else{rows.push(json!({"type":"feed","action":{"type":"route","route":route,"slot":worker.slot,"token":token,"generation":generation,"connected":true,"replacement":worker.replacement,"applied":self.settings.perpetual_symbols}}));}
            },
            Event::Disconnected{route,generation}=>{
                let worker=self.workers.get_mut(&route).unwrap();if worker.generation!=generation||generation==0{return Ok(vec![])}worker.connected=false;
                if worker.spot {rows.push(json!({"type":"spot_route","token":worker.token,"generation":generation,"connected":false,"symbols":self.settings.spot_symbols}));}
                else {rows.push(json!({"type":"feed","action":{"type":"route","route":route,"slot":worker.slot,"token":worker.token,"generation":generation,"connected":false,"replacement":worker.replacement,"applied":[]}}));}
            },
            Event::Frame{route,token,generation,mut payload,clock}=>{
                let processing=self.clock();
                let configured=self.handoff_enabled();
                let worker=self.workers.get_mut(&route).unwrap();if worker.generation!=generation||worker.token!=token{return Err("physical frame token mismatch".into())}
                if configured {crate::reject_numeric_bookticker_fields(&payload)?;}
                let tick=crate::parse_bookticker(&payload,clock.wall_ms,!worker.spot)?;
                self.supplied+=1;
                self.queue_age.record(processing.monotonic_ms-clock.monotonic_ms);self.source_age.record(processing.wall_ms-tick.source_ms);
                if worker.spot{rows.push(json!({"type":"spot_raw","token":token,"generation":generation,"payload":payload,"now_ms":processing.wall_ms,"receive_ms":clock.wall_ms}));}
                else {
                    let membership=*self.memberships.get(&tick.symbol).ok_or("unsubscribed native frame")?;
                    payload["_ptk_connection_id"]=json!(token);payload["_ptk_connection_generation"]=json!(generation);payload["run_time_ms"]=json!(clock.wall_ms);
                    rows.push(json!({"type":"feed","action":{"type":"raw_captured","route":route,"payload":payload,"membership":membership,"serial":self.serials[&route],"wall_ms":processing.wall_ms,"monotonic_ms":processing.monotonic_ms,"ingress_clock":{"wall_ms":clock.wall_ms,"monotonic_ms":clock.monotonic_ms}}}));
                }
            }
        }
        Ok(rows)
    }
    fn receive_event(&self,timeout:Duration)->Result<Option<Event>> {
        match self.receiver.try_recv() {
            Ok(event)=>return Ok(Some(event)),
            Err(mpsc::TryRecvError::Disconnected)=>return Err("native ingress disconnected".into()),
            Err(mpsc::TryRecvError::Empty)=>{},
        }
        if !timeout.is_zero(){self.control.wake.wait(timeout)?;}
        if let Some(error)=self.failure.lock().map_err(|_|"native failure lock poisoned")?.clone(){return Err(error)}
        // One queue check after the single bounded wait, including spurious or
        // control-only wakes; never spin and never consume more than one event.
        match self.receiver.try_recv() {
            Ok(event)=>Ok(Some(event)),Err(mpsc::TryRecvError::Empty)=>Ok(None),
            Err(mpsc::TryRecvError::Disconnected)=>Err("native ingress disconnected".into()),
        }
    }
    pub fn accounting(&self)->Value {let mut report=json!({"wire_raw_received":self.received.load(Ordering::Acquire),"supplied":self.supplied,"retired_frames":self.retired_frames,"retired_events_or_workers":self.retired,"workers":self.workers.len(),"acknowledged":self.workers.values().filter(|w|w.connected).count(),"capacity":CAPACITY,"queue_age":self.queue_age.value(),"source_age":self.source_age.value(),"first_failure":self.failure.lock().ok().and_then(|f|f.clone()),"recv_total":self.recv_total,"channel_wait":self.channel_wait});if let Some(status)=self.handoff_status(){report["configuration"]=status;report["workers"]=json!(self.workers.len()+self.retiring.len());}report}
    pub fn shutdown(&mut self)->Result<()> {
        if self.control.handoff.is_some(){
            let routes=self.workers.keys().cloned().collect();
            if let Err(error)=self.detach_handoff(routes).and_then(|_|self.reap_ready()){return Err(self.latch_handoff_failure(error))}
            if !self.retiring.is_empty(){
                let error=self.latch_handoff_failure("configuration shutdown retains unfinished workers; owner incomplete".into());
                if let Ok(mut retained)=self.control.failure_accounting.lock(){let report=retained.get_or_insert_with(||self.accounting());report["cleanup"]=json!({"active_workers":self.workers.len(),"retiring_workers":self.retiring.len(),"workers_after_shutdown":self.workers.len()+self.retiring.len(),"cleanup_complete":false});}
                return Err(error)
            }
        }
        let mut first=None;for route in self.workers.keys().cloned().collect::<Vec<_>>(){if let Err(e)=self.retire(&route){if first.is_none(){first=Some(e)}}}
        let mut purged=0;while let Ok(event)=self.receiver.try_recv(){if matches!(event,Event::Frame{..}){purged+=1;}else{self.retired+=1;}}
        if purged>0 {let error=format!("shutdown retained {purged} unconsumed native frames; owner incomplete");fail_notified(&self.failure,&self.control.wake,error.clone());if first.is_none(){first=Some(error)}}
        self.retired_frames+=purged;
        if let Ok(mut retained)=self.control.failure_accounting.lock(){if let Some(report)=retained.as_mut(){report["cleanup"]=json!({"workers_after_shutdown":self.workers.len(),"purged_frames":purged,"retired_frames_total":self.retired_frames});if self.control.handoff.is_some(){report["cleanup"]["active_workers"]=json!(self.workers.len());report["cleanup"]["retiring_workers"]=json!(self.retiring.len());report["cleanup"]["cleanup_complete"]=json!(self.workers.is_empty()&&self.retiring.is_empty()&&purged==0);}}}
        if let Some(e)=first{Err(e)}else{Ok(())}
    }
}
impl Drop for Runtime {fn drop(&mut self){
    if let Ok(mut retained)=self.control.failure_accounting.lock(){if retained.is_none(){*retained=Some(self.accounting());}}
    let _=self.shutdown();
    // Final destruction retains ownership. Progress/status never performs this
    // wait; a process forced closed by its facade remains explicitly incomplete.
    while !self.retiring.is_empty(){let _=self.reap_ready();if !self.retiring.is_empty(){thread::sleep(Duration::from_millis(10));}}
}}

#[cfg(test)]
mod wall_clock_tests {
    use super::*;
    use crate::{Action,Selector,parse_bookticker};

    #[test]
    fn wall_ms_matches_all_actual_cpython_31115_ptk_reference_vectors(){
        let mut count=0;let mut floor_differences=0;let mut split_differences=0;
        for line in include_str!("../tests/fixtures/cpython_wall_reference.tsv").lines(){
            let fields:Vec<_>=line.split('|').collect();assert_eq!(fields.len(),4);
            let seconds:u64=fields[0].parse().unwrap();let nanos:u32=fields[1].parse().unwrap();
            let bits=u64::from_str_radix(fields[2],16).unwrap();let expected:i64=fields[3].parse().unwrap();let duration=Duration::new(seconds,nanos);
            assert_eq!(cpython_wall_seconds(duration).unwrap().to_bits(),bits,"C double: {line}");
            assert_eq!(wall_ms_from_duration(duration),expected,"PTK milliseconds: {line}");
            floor_differences+=usize::from(duration.as_millis() as i64!=expected);
            split_differences+=usize::from((seconds as f64+f64::from(nanos)/1_000_000_000.0).to_bits()!=bits);
            count+=1;
        }
        assert_eq!((count,floor_differences,split_differences),(280,155,40));
    }
    #[test]
    fn wall_ms_preserves_even_odd_half_ties_and_second_carry(){
        for (seconds,nanos,expected) in [(0,500_000,0),(0,1_500_000,2),(0,2_500_000,2),(0,3_500_000,4),(0,998_500_000,998),(0,999_500_000,1000),(1,500_000,1000),(1,1_500_000,1002)]{
            assert_eq!(wall_ms_from_duration(Duration::new(seconds,nanos)),expected);
        }
        assert_eq!(wall_ms_from_duration(Duration::new(1_800_000_000,999_999_999)),1_800_000_001_000);
        assert_eq!(cpython_wall_seconds(Duration::new(1_800_000_000,0)).unwrap().to_bits(),1_800_000_000_f64.to_bits());
    }
    fn configure(wall_ms:i64)->Result<Selector>{
        let mut selector=Selector::default();selector.apply_compact(serde_json::from_value(json!({"type":"configure","stream_id":"wall-reference","mode":"single","symbols":["BTCUSDT"],"wall_ms":wall_ms,"monotonic_ms":0})).unwrap())?;Ok(selector)
    }
    #[test]
    fn wall_ms_out_of_reference_domain_and_pre_epoch_cannot_become_valid_clocks(){
        let before_epoch=UNIX_EPOCH-Duration::from_nanos(1);
        assert_eq!(wall_ms_from_duration(before_epoch.duration_since(UNIX_EPOCH).unwrap_or_default()),0);
        assert_eq!(wall_ms_from_duration(Duration::ZERO),0);assert!(configure(0).is_err());
        let last=Duration::from_nanos(i64::MAX as u64);assert!(cpython_wall_seconds(last).is_some());assert!(configure(wall_ms_from_duration(last)).is_err());
        for duration in [last+Duration::from_nanos(1),Duration::new(u64::MAX,999_999_999)]{
            assert!(cpython_wall_seconds(duration).is_none());assert_eq!(wall_ms_from_duration(duration),0);assert!(configure(wall_ms_from_duration(duration)).is_err());
        }
        // The existing selector's time ceiling stays 4e12, independently of
        // the wider conversion-reference domain. No new timestamp is admitted.
        assert!(configure(wall_ms_from_duration(Duration::from_millis(4_000_000_000_000))).is_ok());
        assert!(configure(wall_ms_from_duration(Duration::from_millis(4_000_000_000_001))).is_err());
    }
    #[test]
    fn rounded_wall_retains_strict_future_and_1000_1001_source_receive_gates(){
        let now=wall_ms_from_duration(Duration::new(1_800_000_000,600_000));assert_eq!(now,1_800_000_000_001);
        // source age, receive age, captured wall age, monotonic queue age,
        // expected consumer gate. Backlog uses real captured-ingress semantics.
        for (source_age,receive_age,captured_age,queue_age,accepted) in [(0,0,0,0,true),(-1,0,0,0,false),(0,-1,0,0,false),(1000,0,0,0,true),(1001,0,0,0,false),(1000,1000,1000,1000,true),(1001,1001,1001,1001,false)]{
            let mut selector=configure(now).unwrap();selector.apply_compact(serde_json::from_value(json!({"type":"route","route":"r0","slot":0,"token":"physical","generation":1,"connected":true,"replacement":false,"applied":["BTCUSDT"]})).unwrap()).unwrap();
            let source=now-source_age;let receive=now-receive_age;
            let mut payload=json!({"s":"BTCUSDT","u":1,"E":source,"b":"999999999.99999999","a":"1000000000.00000000","B":"0.00000001","A":"1.23456789","run_time_ms":receive,"_ptk_connection_id":"physical","_ptk_connection_generation":1});
            if source_age==1000&&receive_age==0 {payload.as_object_mut().unwrap().remove("E");payload["T"]=json!(source);}
            let tick=parse_bookticker(&payload,receive,true).unwrap();assert_eq!((tick.source_ms,tick.receive_ms),(source,receive));
            assert_eq!((tick.bid.as_str(),tick.ask.as_str(),tick.bid_size.as_str(),tick.ask_size.as_str()),("999999999.99999999","1000000000.00000000","0.00000001","1.23456789"));
            selector.apply_compact(serde_json::from_value(json!({"type":"raw_captured","route":"r0","payload":payload,"membership":1,"serial":0,"wall_ms":now,"monotonic_ms":queue_age,"ingress_clock":{"wall_ms":now-captured_age,"monotonic_ms":0}})).unwrap()).unwrap();
            let clock=Clock{wall_ms:now,monotonic_ms:queue_age};let frames=selector.apply_compact(Action::Poll{output_capacity:4096,clock}).unwrap().frames;
            assert_eq!(frames.len(),usize::from(accepted),"source age={source_age}, receive age={receive_age}, queue age={queue_age}");
            if accepted {assert_eq!(selector.apply_compact(Action::Gate{frame:frames[0].clone(),clock}).unwrap().accepted,Some(true));}
            else{assert_eq!(selector.state()["symbols"]["BTCUSDT"]["available"],false);}
            if source_age<0||receive_age<0 {assert_eq!(selector.diagnostics()["rejections"]["timestamp"],1);}
        }
    }
}

#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    use crate::{Action,Selector};
    use std::net::TcpListener;
    use tungstenite::Message;

    #[test]
    fn recovery_matches_explicit_ptk_boundaries_and_consumes_connection_lifetime(){
        let mut recovery=Recovery::new(RecoveryPolicy::default());
        for (attempt,delay) in [(1,1000),(2,2000),(3,4000),(4,8000),(5,16000),(6,29750),(6,29750)]{
            recovery.opened(0);assert_eq!(recovery.retry_delay(59999,0),delay);assert_eq!(recovery.attempt,attempt);assert!(recovery.opened_ms.is_none());
        }
        assert_eq!(recovery.retry_delay(60000,250),30000);assert_eq!(recovery.attempt,6);
        recovery.opened(100);assert_eq!(recovery.retry_delay(60100,250),1250);assert_eq!(recovery.attempt,1);assert!(recovery.opened_ms.is_none());
        // Failed handshake after the stable lifetime was consumed cannot reuse it.
        assert_eq!(recovery.retry_delay(60101,0),2000);assert_eq!(recovery.attempt,2);
        recovery.opened(70000);assert_eq!(recovery.retry_delay(69999,0),4000);assert!(recovery.opened_ms.is_none());
        let policy=RecoveryPolicy{base_ms:1000,cap_ms:1100,stable_ms:60000,jitter_ms:250};assert_eq!(policy.spread(),100);let mut capped=Recovery::new(policy);assert_eq!(capped.retry_delay(0,100),1100);assert_eq!(capped.retry_delay(1,100),1100);
    }
    #[test]
    fn rotation_defaults_and_exact_clock_boundaries_match_ptk(){
        let policy=RotationPolicy::default();assert_eq!(policy.initial_due(0,0),79200000);assert_eq!(policy.initial_due(0,1),82800000);assert_eq!(policy.promoted_due(82800000),162000000);assert_eq!(policy.retry_due(79230000),79290000);
        assert!(!policy.expired(100,30099));assert!(policy.expired(100,30100));assert!(!policy.expired(100,99));
        for slot in [0,1]{let due=policy.initial_due(200,slot);assert!(!policy.due(due,due-1));assert!(policy.due(due,due));assert!(policy.due(due,due+1));}assert_eq!(policy.initial_due(200,1)-policy.initial_due(200,0),3600000);
    }
    #[test]
    fn recovery_matches_all_source_extracted_python_reference_vectors(){
        let reference:Value=serde_json::from_str(include_str!("../tests/fixtures/backoff_reference.data")).unwrap();let policy=RecoveryPolicy::default();
        assert_eq!(reference["source_sha256"],"db5ec16637dc31ba5c1a0e70e8236c626b540d43cc12dd8db3da00c01ba5410d");assert_eq!(reference["constants"]["RECONNECT_BASE_DELAY_MS"],policy.base_ms);assert_eq!(reference["constants"]["RECONNECT_MAX_DELAY_MS"],policy.cap_ms);assert_eq!(reference["constants"]["RECONNECT_STABLE_CONNECTION_MS"],policy.stable_ms);assert_eq!(reference["constants"]["PUBLIC_RECONNECT_JITTER_MS"],policy.jitter_ms);
        let cases=reference["cases"].as_array().unwrap();assert_eq!(cases.len(),70);
        for row in cases{let mut recovery=Recovery::new(policy);recovery.attempt=row["attempt_before"].as_u64().unwrap()as u8;if let Some(age)=row["opened_age_ms"].as_u64(){recovery.opened(100000-age);}let delay=recovery.retry_delay(100000,row["jitter_ms"].as_u64().unwrap());assert_eq!(json!(delay),row["delay_ms"]);assert_eq!(json!(recovery.attempt),row["attempt_after"]);assert_eq!(json!(recovery.opened_ms.is_none()),row["lifetime_consumed"]);}
    }
    #[test]
    fn shadow_replacement_uses_slot_specific_highwater_and_complete_ack_targets(){
        for(slot,ready)in [(0,true),(1,false)]{
            let mut selector=Selector::default();let clock=Clock{wall_ms:1800000000000,monotonic_ms:0};selector.apply_compact(serde_json::from_value(json!({"type":"configure","stream_id":"qualification","mode":"shadow","symbols":["BTCUSDT"],"wall_ms":clock.wall_ms,"monotonic_ms":0})).unwrap()).unwrap();
            for(id,route_slot,replacement)in [("primary",0,false),("backup",1,false),("replacement",slot,true)]{selector.apply_compact(serde_json::from_value(json!({"type":"route","route":id,"slot":route_slot,"token":id,"generation":1,"connected":true,"replacement":replacement,"applied":["BTCUSDT"]})).unwrap()).unwrap();}
            for(id,update)in [("primary",10),("backup",20),("replacement",15)]{selector.apply_compact(serde_json::from_value(json!({"type":"raw","route":id,"membership":1,"serial":0,"wall_ms":clock.wall_ms,"monotonic_ms":0,"payload":{"s":"BTCUSDT","u":update,"E":clock.wall_ms,"run_time_ms":clock.wall_ms,"b":"100","a":"101","B":"2","A":"3","_ptk_connection_id":id,"_ptk_connection_generation":1}})).unwrap()).unwrap();}
            selector.apply_compact(Action::Poll{output_capacity:4096,clock}).unwrap();assert_eq!(selector.replacement_ready("replacement",clock).unwrap(),ready);
            selector.apply_compact(serde_json::from_value(json!({"type":"route","route":"replacement","slot":slot,"token":"replacement","generation":1,"connected":true,"replacement":true,"applied":[]})).unwrap()).unwrap();assert!(!selector.replacement_ready("replacement",clock).unwrap());
        }
    }
    struct Fixture {
        endpoint:Endpoint,backup:Endpoint,spot:Endpoint,stop:Arc<AtomicBool>,quotes:Arc<AtomicBool>,withhold:Arc<AtomicBool>,
        arrivals:Arc<Mutex<Vec<(String,u64,Instant)>>>,join:Option<JoinHandle<()>>,
    }
    impl Fixture {
        fn start(close_perp:u64,close_spot:u64)->Self{
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();listener.set_nonblocking(true).unwrap();
            let stop=Arc::new(AtomicBool::new(false));let quotes=Arc::new(AtomicBool::new(true));let withhold=Arc::new(AtomicBool::new(false));let arrivals=Arc::new(Mutex::new(Vec::new()));
            let (server_stop,server_quotes,server_withhold,server_arrivals)=(stop.clone(),quotes.clone(),withhold.clone(),arrivals.clone());
            let join=thread::spawn(move||{
                let mut clients=Vec::new();let counts=Arc::new(Mutex::new(BTreeMap::<String,u64>::new()));let sequence=Arc::new(AtomicU64::new(1));
                while !server_stop.load(Ordering::Acquire){
                    let stream=match listener.accept(){Ok((stream,_))=>stream,Err(e)if e.kind()==std::io::ErrorKind::WouldBlock=>{thread::sleep(Duration::from_millis(2));continue},Err(e)=>panic!("{e}")};
                    let (stop,quotes,withhold,arrivals,counts,sequence)=(server_stop.clone(),server_quotes.clone(),server_withhold.clone(),server_arrivals.clone(),counts.clone(),sequence.clone());
                    clients.push(thread::spawn(move||{
                        stream.set_read_timeout(Some(Duration::from_secs(1))).unwrap();stream.set_write_timeout(Some(Duration::from_millis(250))).unwrap();let mut path=String::new();
                        let mut socket=tungstenite::accept_hdr(stream,|request:&tungstenite::handshake::server::Request,response|{path=request.uri().path().into();Ok(response)}).unwrap();
                        let request:Value=serde_json::from_str(&socket.read().unwrap().into_text().unwrap()).unwrap();let generation=request["id"].as_u64().unwrap();
                        let ordinal={let mut counts=counts.lock().unwrap();let count=counts.entry(path.clone()).or_default();*count+=1;*count};arrivals.lock().unwrap().push((path.clone(),generation,Instant::now()));
                        if socket.send(Message::Text(json!({"id":generation,"result":null}).to_string().into())).is_err(){return}
                        if path=="/perp0"&&ordinal<=close_perp||path=="/spot"&&ordinal<=close_spot {let _=socket.close(None);return}
                        socket.get_mut().set_read_timeout(Some(Duration::from_millis(20))).unwrap();
                        while !stop.load(Ordering::Acquire){
                            if quotes.load(Ordering::Acquire)&&!(path.starts_with("/perp")&&ordinal>1&&withhold.load(Ordering::Acquire)){
                                let now=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64;
                                let quote=json!({"s":"BTCUSDT","u":sequence.fetch_add(1,Ordering::Relaxed),"E":now,"b":"100","a":"101","B":"2","A":"3"});
                                if socket.send(Message::Text(quote.to_string().into())).is_err(){break}
                            }
                            match socket.read(){Err(tungstenite::Error::Io(e))if matches!(e.kind(),std::io::ErrorKind::TimedOut|std::io::ErrorKind::WouldBlock)=>{},Err(_)|Ok(Message::Close(_))=>break,_=>{}}
                        }
                    }));
                }
                for client in clients{client.join().unwrap();}
            });
            Self{endpoint:Endpoint{address,uri:format!("ws://localhost:{}/perp0",address.port())},backup:Endpoint{address,uri:format!("ws://localhost:{}/perp1",address.port())},spot:Endpoint{address,uri:format!("ws://localhost:{}/spot",address.port())},stop,quotes,withhold,arrivals,join:Some(join)}
        }
        fn settings(&self,mode:&str)->Settings{Settings{session:"lifecycle".into(),mode:mode.into(),perpetual:self.endpoint.clone(),spot:self.spot.clone(),perpetual_symbols:vec!["BTCUSDT".into()],spot_symbols:vec!["BTCUSDT".into()],fixture_wall_ms:None,fixture_backup_endpoint:Some(self.backup.clone())}}
        fn quiet(&self,runtime:&mut Runtime){
            self.quotes.store(false,Ordering::Release);let deadline=Instant::now()+Duration::from_secs(3);let mut stable:Option<(Instant,u64)>=None;
            loop{
                let rows=runtime.recv(Duration::from_millis(10)).unwrap();let received=runtime.received.load(Ordering::Acquire);
                // Consume through the real owner until all received raw frames
                // are processed/explicitly retired, then require a stable quiet
                // interval longer than the socket's 250ms receive deadline.
                if rows.is_empty()&&received==runtime.supplied+runtime.retired_frames{
                    if let Some((since,watermark))=stable{if watermark==received{if since.elapsed()>=Duration::from_millis(300){return}}else{stable=Some((Instant::now(),received));}}
                    else{stable=Some((Instant::now(),received));}
                }else{stable=None;}
                assert!(Instant::now()<deadline,"fixture did not quiesce/drain: {}",runtime.accounting());
            }
        }
    }
    impl Drop for Fixture {fn drop(&mut self){self.stop.store(true,Ordering::Release);if let Some(join)=self.join.take(){join.join().unwrap();}}}
    fn selector(mode:&str)->Selector{
        let mut selector=Selector::default();let now=SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64;
        selector.apply(serde_json::from_value(json!({"type":"configure","stream_id":"lifecycle","mode":mode,"symbols":["BTCUSDT"],"wall_ms":now,"monotonic_ms":0})).unwrap()).unwrap();selector
    }
    fn drive(runtime:&mut Runtime,selector:&mut Selector){
        let rows=runtime.recv(Duration::from_millis(5)).unwrap();let control=runtime.control();
        for row in rows{
            let truth=control.physical.lock().unwrap();let removed:Vec<_>=selector.route_ids().filter(|id|!truth.contains_key(*id)).map(str::to_owned).collect();for id in removed{selector.retire_route_control(&id).unwrap();}
            for state in truth.values().filter(|s|s["spot"]!=true){let mut state=state.clone();state.as_object_mut().unwrap().remove("spot");let Action::Route{state}=serde_json::from_value(state).unwrap()else{panic!("not route")};selector.apply_route_control(state.into()).unwrap();}
            if row["type"]=="feed"&&!matches!(row["action"]["type"].as_str(),Some("route"|"retire")){selector.apply_compact(serde_json::from_value(row["action"].clone()).unwrap()).unwrap();}
        }
        let clock=runtime.clock();let truth=control.physical.lock().unwrap();let result=selector.apply_compact(Action::Poll{output_capacity:4096,clock}).unwrap();for frame in result.frames{selector.apply_compact(Action::Gate{frame,clock}).unwrap();}
        for state in truth.values().filter(|s|s["spot"]!=true&&s["replacement"]==true){let id=state["route"].as_str().unwrap();if selector.replacement_ready(id,clock).unwrap(){*control.promotion.lock().unwrap()=Some(id.into());}}
    }
    #[test]
    fn actual_socket_repeated_ack_close_increases_retry_delay_and_spot_reconnects(){
        let fixture=Fixture::start(3,1);let policy=LifecyclePolicy{recovery:RecoveryPolicy{base_ms:25,cap_ms:200,stable_ms:60000,jitter_ms:0},..LifecyclePolicy::default()};let mut runtime=Runtime::start_with_policy(fixture.settings("single"),policy).unwrap();let deadline=Instant::now()+Duration::from_secs(3);
        while Instant::now()<deadline{runtime.recv(Duration::from_millis(5)).unwrap();if runtime.workers.values().any(|w|!w.spot&&w.generation>=4&&w.connected)&&runtime.workers.values().any(|w|w.spot&&w.generation>=2&&w.connected){break}}
        assert!(runtime.workers.values().any(|w|!w.spot&&w.generation==4&&w.connected));assert!(runtime.workers.values().any(|w|w.spot&&w.generation==2&&w.connected));
        let arrivals=fixture.arrivals.lock().unwrap();let perp:Vec<_>=arrivals.iter().filter(|(p,_,_)|p=="/perp0").collect();assert_eq!(perp.len(),4);for(i,minimum)in [(1,25),(2,50),(3,100)]{assert!(perp[i].2.duration_since(perp[i-1].2)>=Duration::from_millis(minimum));}drop(arrivals);
        fixture.quiet(&mut runtime);runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
    }
    #[test]
    fn actual_automatic_staggered_rotation_waits_for_qualified_promotion(){
        for mode in ["shadow","redundant"]{
            let fixture=Fixture::start(0,0);let rotation=RotationPolicy{rotation_ms:300,stagger_ms:100,timeout_ms:150,retry_ms:100};let mut runtime=Runtime::start_with_policy(fixture.settings(mode),LifecyclePolicy{rotation,..LifecyclePolicy::default()}).unwrap();let mut selector=selector(mode);let initial:Vec<_>=(0..2).map(|slot|runtime.workers.iter().find(|(_,w)|!w.spot&&w.slot==slot&&!w.replacement).unwrap().0.clone()).collect();let deadline=Instant::now()+Duration::from_secs(3);let mut starts=Vec::new();let mut seen=BTreeSet::new();let mut completed=BTreeSet::new();
            while Instant::now()<deadline{drive(&mut runtime,&mut selector);assert!(runtime.workers.values().filter(|w|w.replacement).count()<=1);assert!(runtime.workers.values().filter(|w|!w.spot).count()<=3);
                for (id,w)in &runtime.workers{if w.replacement&&seen.insert(id.clone()){starts.push((w.slot,w.created_ms));assert!(runtime.workers.contains_key(&initial[w.slot as usize]));}if !w.spot&&!w.replacement&&id!=&initial[w.slot as usize]{completed.insert(w.slot);}}
                if completed.len()==2{break}
            }
            assert_eq!(completed.len(),2);assert_eq!(starts[0].0,0);assert!(starts[0].1>=300);assert_eq!(starts[1].0,1);assert!(starts[1].1>=400);
            // Prevent another accelerated cycle during deterministic drain.
            for w in runtime.workers.values_mut(){w.due_ms=u64::MAX;}
            fixture.quiet(&mut runtime);runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
        }
    }
    #[test]
    fn actual_ack_without_quotes_times_out_only_replacement_and_schedules_retry(){
        let fixture=Fixture::start(0,0);fixture.withhold.store(true,Ordering::Release);let rotation=RotationPolicy{timeout_ms:80,retry_ms:180,..RotationPolicy::default()};let mut runtime=Runtime::start_with_policy(fixture.settings("single"),LifecyclePolicy{rotation,..LifecyclePolicy::default()}).unwrap();let mut selector=selector("single");let old=runtime.workers.iter().find(|(_,w)|!w.spot).unwrap().0.clone();
        let deadline=Instant::now()+Duration::from_secs(2);while !runtime.workers[&old].connected&&Instant::now()<deadline{drive(&mut runtime,&mut selector);}assert!(runtime.workers[&old].connected);
        runtime.rotate(0).unwrap();let replacement=runtime.workers.iter().find(|(_,w)|w.replacement).unwrap().0.clone();let created=runtime.workers[&replacement].created_ms;let mut saw_ack=false;
        while runtime.workers.contains_key(&replacement)&&Instant::now()<deadline{drive(&mut runtime,&mut selector);assert!(runtime.workers.contains_key(&old));if let Some(w)=runtime.workers.get(&replacement){saw_ack|=w.connected;}assert!(runtime.control.promotion.lock().unwrap().is_none());}
        assert!(saw_ack);assert!(!runtime.workers.contains_key(&replacement));assert!(runtime.workers[&old].due_ms>=created+80+180);assert!(runtime.workers[&old].connected);
        fixture.quiet(&mut runtime);runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
    }
    #[test]
    fn actual_qualified_feedback_wins_over_timeout_at_the_same_monotonic_boundary(){
        let fixture=Fixture::start(0,0);let rotation=RotationPolicy{timeout_ms:200,..RotationPolicy::default()};let mut runtime=Runtime::start_with_policy(fixture.settings("single"),LifecyclePolicy{rotation,..LifecyclePolicy::default()}).unwrap();let mut selector=selector("single");let old=runtime.workers.iter().find(|(_,w)|!w.spot).unwrap().0.clone();let deadline=Instant::now()+Duration::from_secs(2);
        while !runtime.workers[&old].connected&&Instant::now()<deadline{drive(&mut runtime,&mut selector);}runtime.rotate(0).unwrap();let replacement=runtime.workers.iter().find(|(_,w)|w.replacement).unwrap().0.clone();
        while runtime.control.promotion.lock().unwrap().is_none()&&Instant::now()<deadline{drive(&mut runtime,&mut selector);}assert!(runtime.control.promotion.lock().unwrap().is_some());
        let timeout=runtime.workers[&replacement].created_ms+rotation.timeout_ms;while (runtime.origin.elapsed().as_millis() as u64)<timeout{thread::sleep(Duration::from_millis(2));}
        // Keep the observed qualified quote within freshness. Worker truth is
        // current; no transport continuity after this decision is asserted.
        assert!((runtime.origin.elapsed().as_millis() as u64)<timeout+500);runtime.recv(Duration::ZERO).unwrap();assert!(!runtime.workers.contains_key(&old));assert!(!runtime.workers[&replacement].replacement);
        fixture.quiet(&mut runtime);runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
    }
    #[test]
    fn known_invalid_physical_promotion_and_first_failure_never_retire_old_route(){
        let fixture=Fixture::start(0,0);let mut runtime=Runtime::start(fixture.settings("single")).unwrap();let mut selector=selector("single");let old=runtime.workers.iter().find(|(_,w)|!w.spot).unwrap().0.clone();let deadline=Instant::now()+Duration::from_secs(2);
        while !runtime.workers[&old].connected&&Instant::now()<deadline{drive(&mut runtime,&mut selector);}runtime.rotate(0).unwrap();let replacement=runtime.workers.iter().find(|(_,w)|w.replacement).unwrap().0.clone();
        while runtime.control.promotion.lock().unwrap().is_none()&&Instant::now()<deadline{drive(&mut runtime,&mut selector);}assert!(runtime.control.promotion.lock().unwrap().is_some());assert!(runtime.workers[&replacement].connected);
        let good=runtime.control.physical.lock().unwrap()[&replacement].clone();
        for(field,value)in [("connected",json!(false)),("token",json!("retired-token")),("generation",json!(999)),("applied",json!(["OTHERUSDT"]))] {
            runtime.control.physical.lock().unwrap().get_mut(&replacement).unwrap()[field]=value;*runtime.control.promotion.lock().unwrap()=Some(replacement.clone());runtime.recv(Duration::ZERO).unwrap();
            assert!(runtime.workers.contains_key(&old));assert!(runtime.workers[&replacement].replacement);assert!(runtime.workers[&replacement].connected);runtime.control.physical.lock().unwrap().insert(replacement.clone(),good.clone());
        }
        *runtime.control.failure.lock().unwrap()=Some("retained-first-failure".into());*runtime.control.promotion.lock().unwrap()=Some(replacement.clone());assert_eq!(runtime.recv(Duration::ZERO).unwrap_err(),"retained-first-failure");assert!(runtime.workers.contains_key(&old));assert!(runtime.workers[&replacement].replacement);
        fixture.quotes.store(false,Ordering::Release);let _=runtime.shutdown();assert_eq!(runtime.accounting()["workers"],0);assert_eq!(runtime.control.failure.lock().unwrap().as_deref(),Some("retained-first-failure"));
    }
    #[test]
    fn actual_socket_shutdown_interrupts_maximum_retry_wait_without_an_extra_attempt(){
        let fixture=Fixture::start(1,0);let policy=LifecyclePolicy{recovery:RecoveryPolicy{base_ms:30000,cap_ms:30000,stable_ms:60000,jitter_ms:0},..LifecyclePolicy::default()};let mut runtime=Runtime::start_with_policy(fixture.settings("single"),policy).unwrap();let deadline=Instant::now()+Duration::from_secs(2);
        loop{runtime.recv(Duration::from_millis(5)).unwrap();if runtime.workers.values().any(|w|!w.spot&&w.generation==1&&!w.connected){break}assert!(Instant::now()<deadline);}
        fixture.quiet(&mut runtime);let stopping=Instant::now();runtime.shutdown().unwrap();assert!(stopping.elapsed()<Duration::from_secs(1));assert_eq!(runtime.accounting()["workers"],0);assert_eq!(fixture.arrivals.lock().unwrap().iter().filter(|(path,_,_)|path=="/perp0").count(),1);
    }
}

#[cfg(test)]
mod handoff_tests {
    use super::*;
    use handoff::Phase;
    // Declared after Runtime so assertion unwinding releases the worker before
    // Runtime's ownership-preserving destructor can wait for it.
    struct Release(Arc<AtomicBool>);
    impl Drop for Release {fn drop(&mut self){self.0.store(true,Ordering::Release);}}
    pub(super) fn empty_runtime()->Runtime {
        let settings:Settings=serde_json::from_value(json!({"session":"handoff","mode":"single","perpetual":{"address":"127.0.0.1:1","uri":"ws://127.0.0.1:1"},"spot":{"address":"127.0.0.1:1","uri":"ws://127.0.0.1:1"},"perpetual_symbols":["ETHUSDT"],"spot_symbols":["ETHUSDT"]})).unwrap();
        let control=Control::default();let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        Runtime{settings,workers:BTreeMap::new(),sender,receiver,failure:control.failure.clone(),origin:control.origin,next:0,generations:BTreeMap::new(),memberships:BTreeMap::from([("ETHUSDT".into(),1)]),serials:BTreeMap::new(),supplied:0,retired:0,fixture_wall:control.fixture_wall.clone(),anchored:false,received:Arc::new(AtomicU64::new(0)),retired_frames:0,queue_age:AgeStats::default(),source_age:AgeStats::default(),control,recv_total:Timing::default(),channel_wait:Timing::default(),lifecycle:LifecyclePolicy::default(),retiring:BTreeMap::new(),pending_targets:None}
    }
    #[test]
    fn configuration_retirement_progress_keeps_status_responsive_with_real_unfinished_handle(){
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();
        let release=Arc::new(AtomicBool::new(false));let _release=Release(release.clone());
        let stopped=Arc::new(AtomicBool::new(false));let physical=runtime.control.physical.clone();
        physical.lock().unwrap().insert("old".into(),json!({"token":"old-token","generation":1,"connected":true,"applied":["ETHUSDT"]}));
        let resurrected=Arc::new(AtomicBool::new(false));let checked=resurrected.clone();
        let join=thread::spawn(move||{while !release.load(Ordering::Acquire){thread::sleep(Duration::from_millis(1));}if let Some(row)=physical.lock().unwrap().get_mut("old"){row["connected"]=json!(true);checked.store(true,Ordering::Release);}});
        runtime.workers.insert("old".into(),Worker{stop:stopped.clone(),join:Some(join),slot:0,spot:false,replacement:false,token:"old-token".into(),generation:1,connected:true,created_ms:0,due_ms:u64::MAX});
        let targets=Targets{perpetual:vec!["BTCUSDT".into()],spot:vec![]};runtime.prepare_handoff(1,targets.clone()).unwrap();
        let slot=runtime.control.handoff.clone().unwrap();
        assert_eq!(slot.lock().unwrap().status().0.unwrap().phase,Phase::Prepared);
        assert!(runtime.progress_handoff().unwrap().is_empty());assert_eq!(runtime.workers.len(),1);assert!(!stopped.load(Ordering::Acquire));
        let installed=Arc::new(AtomicBool::new(false));let marker=installed.clone();
        slot.lock().unwrap().commit_logical(1,||{assert_eq!(runtime.workers.len(),1);marker.store(true,Ordering::Release);Ok(())}).unwrap();
        let start=Instant::now();let rows=runtime.progress_handoff().unwrap();
        assert!(installed.load(Ordering::Acquire));assert!(start.elapsed()<Duration::from_millis(250));assert_eq!(rows.len(),2);
        assert_eq!((runtime.workers.len(),runtime.retiring.len()),(0,1));assert!(stopped.load(Ordering::Acquire));assert!(runtime.control.physical.lock().unwrap().is_empty());
        let status_start=Instant::now();assert_eq!(runtime.handoff_status().unwrap()["retiring_workers"],1);assert!(status_start.elapsed()<Duration::from_millis(250));assert!(!runtime.retiring["old"].join.as_ref().unwrap().is_finished());
        runtime.sender.send(Event::Connected{route:"old".into(),token:"late".into(),generation:2}).unwrap();
        runtime.sender.send(Event::Frame{route:"old".into(),token:"old-token".into(),generation:1,payload:json!({}),clock:runtime.clock()}).unwrap();
        assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());
        assert_eq!((runtime.supplied,runtime.retired_frames),(0,1));assert_eq!(runtime.retired,3);
        fail(&runtime.failure,"original failure before shutdown".into());
        assert_eq!(runtime.shutdown().unwrap_err(),"original failure before shutdown");assert_eq!(runtime.retiring.len(),1);
        let retained=runtime.control.failure_accounting.lock().unwrap().clone().unwrap();assert_eq!(retained["cleanup"]["cleanup_complete"],false);assert_eq!(retained["cleanup"]["workers_after_shutdown"],1);assert_eq!(retained["cleanup"]["retiring_workers"],1);assert_eq!(runtime.accounting()["workers"],1);
        assert_eq!(slot.lock().unwrap().status().0.unwrap().first_failure.as_deref(),Some("original failure before shutdown"));
        _release.0.store(true,Ordering::Release);
        let deadline=Instant::now()+Duration::from_secs(2);while runtime.retiring["old"].join.as_ref().is_some_and(|j|!j.is_finished()){assert!(Instant::now()<deadline);thread::sleep(Duration::from_millis(1));}
        runtime.shutdown().unwrap();assert_eq!(runtime.retiring.len(),0);assert!(!resurrected.load(Ordering::Acquire));
    }
    #[test]
    fn configuration_reordered_targets_preserve_worker_identity_and_due_time(){
        let mut runtime=empty_runtime();runtime.settings.perpetual_symbols=vec!["ETHUSDT".into(),"BTCUSDT".into()];runtime.settings.spot_symbols=vec!["ETHUSDT".into(),"BTCUSDT".into()];runtime.memberships.insert("BTCUSDT".into(),1);runtime.enable_handoff().unwrap();
        runtime.workers.insert("old".into(),Worker{stop:Arc::new(AtomicBool::new(false)),join:None,slot:0,spot:false,replacement:false,token:"token".into(),generation:7,connected:true,created_ms:4,due_ms:99});
        runtime.control.physical.lock().unwrap().insert("old".into(),json!({"token":"token","generation":7,"connected":true,"applied":["ETHUSDT","BTCUSDT"]}));
        runtime.workers.insert("spot".into(),Worker{stop:Arc::new(AtomicBool::new(false)),join:None,slot:0,spot:true,replacement:false,token:"spot-token".into(),generation:8,connected:true,created_ms:5,due_ms:100});
        runtime.control.physical.lock().unwrap().insert("spot".into(),json!({"token":"spot-token","generation":8,"connected":true,"applied":["ETHUSDT","BTCUSDT"]}));
        let memberships=runtime.memberships.clone();let truth=runtime.control.physical.lock().unwrap().clone();
        runtime.prepare_handoff(1,Targets{perpetual:vec!["BTCUSDT".into(),"ETHUSDT".into()],spot:vec!["BTCUSDT".into(),"ETHUSDT".into()]}).unwrap();
        runtime.control.handoff.as_ref().unwrap().lock().unwrap().commit_logical(1,||Ok(())).unwrap();
        assert!(runtime.progress_handoff().unwrap().is_empty());assert_eq!(runtime.memberships,memberships);assert_eq!(*runtime.control.physical.lock().unwrap(),truth);
        assert_eq!((runtime.workers["old"].token.as_str(),runtime.workers["old"].generation,runtime.workers["old"].due_ms),("token",7,99));assert!(runtime.retiring.is_empty());
        assert_eq!(runtime.control.handoff.as_ref().unwrap().lock().unwrap().status().0.unwrap().phase,Phase::Ready);assert!(runtime.handoff_fenced());
        runtime.control.handoff.as_ref().unwrap().lock().unwrap().commit_logical(1,||panic!("duplicate logical mutation")).unwrap();assert!(runtime.progress_handoff().unwrap().is_empty());
    }
    #[test]
    fn configuration_preexisting_failure_wins_without_side_effects(){
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();runtime.prepare_handoff(1,Targets{perpetual:vec!["BTCUSDT".into()],spot:vec![]}).unwrap();
        fail(&runtime.failure,"original transport failure".into());
        assert_eq!(runtime.progress_handoff().unwrap_err(),"original transport failure");
        let slot=runtime.control.handoff.as_ref().unwrap().lock().unwrap();assert_eq!(slot.status().0.unwrap().phase,Phase::Failed);assert_eq!(slot.first_failure().as_deref(),Some("original transport failure"));
        assert_eq!(runtime.settings.perpetual_symbols,["ETHUSDT"]);assert!(runtime.pending_targets.is_none());
    }
    #[test]
    fn configuration_slot_is_bounded_pure_and_preserves_first_failure(){
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();let before=runtime.target_state();
        let targets=Targets{perpetual:vec!["BTCUSDT".into()],spot:vec![]};runtime.prepare_handoff(1,targets.clone()).unwrap();
        assert_eq!(before,runtime.target_state());assert_eq!(runtime.settings.perpetual_symbols,["ETHUSDT"]);assert_eq!(runtime.memberships.len(),1);
        assert!(runtime.prepare_handoff(2,targets.clone()).is_err());let slot=runtime.control.handoff.clone().unwrap();
        slot.lock().unwrap().abort(1).unwrap();runtime.prepare_handoff(2,targets.clone()).unwrap();
        assert_eq!(slot.lock().unwrap().status().1.unwrap().phase,Phase::Aborted);
        assert!(slot.lock().unwrap().commit_logical(2,||Err("first logical failure".into())).is_err());
        slot.lock().unwrap().fail("later failure".into());assert_eq!(slot.lock().unwrap().status().0.unwrap().first_failure.as_deref(),Some("first logical failure"));
        assert!(slot.lock().unwrap().fenced());assert_eq!(runtime.progress_handoff().unwrap_err(),"first logical failure");assert!(runtime.pending_targets.is_none());
        assert!(runtime.prepare_handoff(3,targets).is_err());
    }
}


#[cfg(test)]
mod ingress_emit_tests {
    use super::*;

    // Test-only owned payload marker, carried by the same emit monomorphization
    // seam. No production Event field or transport behavior is invented.
    struct Owned {
        id:usize,
        failure:Arc<Mutex<Option<String>>>,
        released:Arc<Mutex<Vec<(usize,Option<String>)>>>,
    }
    impl Owned {
        fn new(id:usize,failure:&Arc<Mutex<Option<String>>>,released:&Arc<Mutex<Vec<(usize,Option<String>)>>>)->Self {
            Self{id,failure:failure.clone(),released:released.clone()}
        }
    }
    impl Drop for Owned {
        fn drop(&mut self) {
            let cause=self.failure.lock().unwrap_or_else(|poisoned|poisoned.into_inner()).clone();
            self.released.lock().unwrap_or_else(|poisoned|poisoned.into_inner()).push((self.id,cause));
        }
    }

    #[test]
    fn successful_emit_keeps_actual_event_payload_tokens_and_clock() {
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let failure=Mutex::new(None);
        let original=json!({"s":"ETHUSDT","u":17,"b":"2000.01","a":"2000.02","B":"3.25","A":"4.50","E":1793771321534_i64,"T":1793771321533_i64});
        assert!(emit(&sender,&failure,Event::Frame{route:"perp0".into(),token:"actual-token".into(),generation:3,payload:original.clone(),clock:Clock{wall_ms:1793771321539,monotonic_ms:7}}));
        let cause=failure.lock().unwrap().clone();assert!(cause.is_none());
        match receiver.try_recv().unwrap() {
            Event::Frame{route,token,generation,payload,clock}=>{
                assert_eq!((route.as_str(),token.as_str(),generation),("perp0","actual-token",3));
                assert_eq!(payload,original);
                assert_eq!((clock.wall_ms,clock.monotonic_ms),(1793771321539,7));
            },
            _=>panic!("successful emit changed the actual event variant"),
        }
        assert!(matches!(receiver.try_recv(),Err(mpsc::TryRecvError::Empty)));
    }

    #[test]
    fn actual_full_channel_latches_full_and_releases_owned_events_once() {
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let failure=Arc::new(Mutex::new(None));
        let released=Arc::new(Mutex::new(Vec::new()));
        for id in 0..CAPACITY {assert!(emit(&sender,&failure,Owned::new(id,&failure,&released)));}
        let released_empty=released.lock().unwrap().is_empty();assert!(released_empty);
        assert!(!emit(&sender,&failure,Owned::new(CAPACITY,&failure,&released)));
        let cause=failure.lock().unwrap().clone();
        assert_eq!(cause.as_deref(),Some("native ingress full; owner incomplete"));
        let released_before_cleanup=released.lock().unwrap().clone();
        assert_eq!(released_before_cleanup,vec![(CAPACITY,None)]);
        // Receiver retirement owns all queued values; no send/error path cloned,
        // leaked or double-released either the rejected or queued payloads.
        drop(receiver);
        assert!(!emit(&sender,&failure,Owned::new(CAPACITY+1,&failure,&released)));
        let cause=failure.lock().unwrap().clone();
        assert_eq!(cause.as_deref(),Some("native ingress full; owner incomplete"));
        let rows=released.lock().unwrap().clone();
        let mut ids:Vec<_>=rows.iter().map(|(id,_)|*id).collect();ids.sort_unstable();
        assert_eq!(ids,(0..CAPACITY+2).collect::<Vec<_>>());
        assert!(rows.iter().filter(|(id,_)|*id!=CAPACITY).all(|(_,reason)|reason.as_deref()==Some("native ingress full; owner incomplete")));
    }

    #[test]
    fn actual_dropped_receiver_latches_disconnected_and_releases_failed_event_once() {
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let failure=Arc::new(Mutex::new(None));let released=Arc::new(Mutex::new(Vec::new()));
        drop(receiver);
        assert!(!emit(&sender,&failure,Owned::new(9,&failure,&released)));
        let cause=failure.lock().unwrap().clone();
        assert_eq!(cause.as_deref(),Some("native ingress receiver disconnected; owner incomplete"));
        let released_before_cleanup=released.lock().unwrap().clone();
        assert_eq!(released_before_cleanup,vec![(9,None)]);
        assert!(!emit(&sender,&failure,Owned::new(10,&failure,&released)));
        let rows=released.lock().unwrap().clone();
        assert_eq!(rows.len(),2);assert_eq!(rows[1],(10,Some("native ingress receiver disconnected; owner incomplete".into())));
    }

    #[test]
    fn preexisting_first_failure_survives_both_typed_errors_and_success() {
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let failure=Arc::new(Mutex::new(Some("original physical failure".into())));
        let released=Arc::new(Mutex::new(Vec::new()));
        for id in 0..CAPACITY {assert!(emit(&sender,&failure,Owned::new(id,&failure,&released)));}
        assert!(!emit(&sender,&failure,Owned::new(CAPACITY,&failure,&released)));
        drop(receiver);
        assert!(!emit(&sender,&failure,Owned::new(CAPACITY+1,&failure,&released)));
        let cause=failure.lock().unwrap().clone();
        assert_eq!(cause.as_deref(),Some("original physical failure"));
        let rows=released.lock().unwrap().clone();assert_eq!(rows.len(),CAPACITY+2);
        assert!(rows.iter().all(|(_,reason)|reason.as_deref()==Some("original physical failure")));
        let mut ids:Vec<_>=rows.iter().map(|(id,_)|*id).collect();ids.sort_unstable();
        assert_eq!(ids,(0..CAPACITY+2).collect::<Vec<_>>());
    }

    #[test]
    fn receiver_cleanup_releases_queued_owned_events_without_a_failure() {
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let failure=Arc::new(Mutex::new(None));let released=Arc::new(Mutex::new(Vec::new()));
        assert!(emit(&sender,&failure,Owned::new(1,&failure,&released)));
        assert!(emit(&sender,&failure,Owned::new(2,&failure,&released)));
        let released_empty=released.lock().unwrap().is_empty();assert!(released_empty);drop(receiver);drop(sender);
        let rows=released.lock().unwrap().clone();let mut ids:Vec<_>=rows.iter().map(|(id,_)|*id).collect();ids.sort_unstable();
        assert_eq!(ids,vec![1,2]);assert!(rows.iter().all(|(_,reason)|reason.is_none()));
        let cause=failure.lock().unwrap().clone();assert!(cause.is_none());
    }
}

#[cfg(test)]
mod control_wakeup_tests {
    use super::*;
    use super::handoff_tests::empty_runtime;

    #[test]
    fn notification_before_park_is_retained_and_bounded() {
        let wake=WakeSignal::default();
        for _ in 0..4096 {wake.notify();}
        assert!(*wake.state.0.lock().unwrap());
        wake.wait(Duration::from_secs(1)).unwrap();
        assert!(!*wake.state.0.lock().unwrap());
        // Exactly one retained bit; an empty zero wait does not loop.
        wake.wait(Duration::ZERO).unwrap();
        assert!(!*wake.state.0.lock().unwrap());
    }
    #[test]
    fn notification_during_atomic_park_has_no_lost_wake() {
        let wake=WakeSignal::default();let receiver=wake.clone();
        let (ready,started)=mpsc::sync_channel(1);let (done,finished)=mpsc::sync_channel(1);
        let join=thread::spawn(move||{
            let pending=receiver.state.0.lock().unwrap();
            ready.send(()).unwrap();
            receiver.wait_pending(pending,Duration::from_secs(2)).unwrap();
            done.send(()).unwrap();
        });
        started.recv_timeout(Duration::from_secs(1)).unwrap();
        // Acquiring the same mutex cannot pass the waiter's check-to-park gap.
        wake.notify();finished.recv_timeout(Duration::from_secs(1)).unwrap();join.join().unwrap();
        assert!(!*wake.state.0.lock().unwrap());
    }
    #[test]
    fn runtime_moves_between_receivers_and_consumes_one_queued_event_per_recv() {
        let mut runtime=empty_runtime();let wake=runtime.control.wake.clone();
        runtime.control.wake.notify();
        assert!(runtime.recv(Duration::from_secs(1)).unwrap().is_empty());
        let join=thread::spawn(move||{
            wake.notify();assert!(runtime.recv(Duration::from_secs(1)).unwrap().is_empty());
            for generation in 1..=2 {assert!(emit_notified(&runtime.sender,&runtime.failure,&runtime.control.wake,Event::Disconnected{route:"retired".into(),generation}));}
            assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());assert_eq!(runtime.retired,1);
            assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());assert_eq!(runtime.retired,2);
            runtime
        });
        let mut runtime=join.join().unwrap();runtime.control.wake.notify();
        assert!(runtime.recv(Duration::from_secs(1)).unwrap().is_empty());
    }
    #[test]
    fn empty_spurious_wake_and_timeout_are_single_bounded_waits() {
        let mut runtime=empty_runtime();runtime.control.wake.notify();
        assert!(runtime.recv(Duration::from_secs(1)).unwrap().is_empty());
        assert_eq!(runtime.channel_wait.calls,1);
        assert!(runtime.recv(Duration::from_millis(1)).unwrap().is_empty());
        assert_eq!(runtime.channel_wait.calls,2);assert_eq!(runtime.supplied,0);
    }
    #[test]
    fn first_failure_and_emit_error_notify_without_replacing_cause() {
        let runtime=empty_runtime();
        fail_notified(&runtime.failure,&runtime.control.wake,"original".into());
        fail_notified(&runtime.failure,&runtime.control.wake,"later".into());
        assert_eq!(runtime.failure.lock().unwrap().as_deref(),Some("original"));
        assert!(*runtime.control.wake.state.0.lock().unwrap());
        let (sender,receiver)=mpsc::sync_channel::<u8>(CAPACITY);drop(receiver);
        let failure=Mutex::new(None);let wake=WakeSignal::default();
        assert!(!emit_notified(&sender,&failure,&wake,1));
        assert_eq!(failure.lock().unwrap().as_deref(),Some("native ingress receiver disconnected; owner incomplete"));
        assert!(*wake.state.0.lock().unwrap());
    }
}

#[cfg(test)]
mod foundation_promotion_tests {
    use super::*;
    use std::net::{Shutdown, TcpListener, TcpStream};
    use tungstenite::Message;

    const RACE_LIMIT:Duration=Duration::from_secs(2);
    const ACK_LIMIT:Duration=Duration::from_secs(3);
    const FIXTURE_HORIZON:Duration=Duration::from_secs(10);

    struct Pause {
        physical:Arc<Mutex<BTreeMap<String,Value>>>,route:String,
        reached:SyncSender<()>,resume:Receiver<()>,
    }
    static PAUSE:Mutex<Option<Pause>>=Mutex::new(None);

    // Only this exact physical owner and route can consume the test pause.
    // No physical mutex is held across the independent disconnect mutation.
    pub(super) fn pause_after_qualified(control:&Control,route:&str) {
        let pause={
            let mut slot=PAUSE.lock().unwrap_or_else(|error|error.into_inner());
            if slot.as_ref().is_some_and(|pause|Arc::ptr_eq(&pause.physical,&control.physical)&&pause.route==route){slot.take()}else{None}
        };
        if let Some(pause)=pause {
            pause.reached.send(()).expect("promotion pause observer disappeared");
            pause.resume.recv_timeout(RACE_LIMIT).expect("promotion disconnect mutation did not resume");
        }
    }

    struct PauseRegistration(Arc<Mutex<BTreeMap<String,Value>>>);
    impl Drop for PauseRegistration {
        fn drop(&mut self) {
            let mut slot=PAUSE.lock().unwrap_or_else(|error|error.into_inner());
            if slot.as_ref().is_some_and(|pause|Arc::ptr_eq(&pause.physical,&self.0)){slot.take();}
        }
    }

    struct MutationJoin(Option<JoinHandle<std::result::Result<bool,String>>>);
    impl MutationJoin {
        fn finish(&mut self)->std::result::Result<bool,String> {
            self.0.take().ok_or("mutation thread already joined")?.join().map_err(|_|"promotion mutation thread panicked".to_string())?
        }
    }
    impl Drop for MutationJoin {fn drop(&mut self){if self.0.is_some(){let _=self.finish();}}}

    struct Fixture {
        endpoint:Endpoint,stop:Arc<AtomicBool>,active:Arc<Mutex<Vec<TcpStream>>>,
        join:Option<JoinHandle<Vec<std::result::Result<(),String>>>>,
    }
    impl Fixture {
        fn start()->Self {
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();
            let address=listener.local_addr().unwrap();listener.set_nonblocking(true).unwrap();
            let stop=Arc::new(AtomicBool::new(false));let active=Arc::new(Mutex::new(Vec::new()));
            let (server_stop,server_active)=(stop.clone(),active.clone());
            let join=thread::spawn(move||{
                let until=Instant::now()+FIXTURE_HORIZON;let mut clients=Vec::new();let mut results=Vec::new();
                while !server_stop.load(Ordering::Acquire)&&Instant::now()<until&&clients.len()<3 {
                    let stream=match listener.accept() {
                        Ok((stream,_))=>stream,
                        Err(error)if error.kind()==std::io::ErrorKind::WouldBlock=>{thread::sleep(Duration::from_millis(2));continue},
                        Err(error)=>{results.push(Err(error.to_string()));break},
                    };
                    let stop=server_stop.clone();let active=server_active.clone();
                    clients.push(thread::spawn(move||serve(stream,stop,active)));
                }
                results.extend(clients.into_iter().map(|join|join.join().unwrap_or_else(|_|Err("promotion fixture client panicked".into()))));results
            });
            Self{endpoint:Endpoint{address,uri:format!("ws://localhost:{}/promotion",address.port())},stop,active,join:Some(join)}
        }
        fn settings(&self)->Settings {
            Settings{session:"foundation-promotion".into(),mode:"single".into(),perpetual:self.endpoint.clone(),spot:self.endpoint.clone(),perpetual_symbols:vec!["BTCUSDT".into()],spot_symbols:vec!["BTCUSDT".into()],fixture_wall_ms:None,fixture_backup_endpoint:None}
        }
        fn finish(&mut self)->std::result::Result<(),String> {
            self.stop.store(true,Ordering::Release);
            for stream in self.active.lock().unwrap_or_else(|error|error.into_inner()).iter(){let _=stream.shutdown(Shutdown::Both);}
            let clients=self.join.take().ok_or("promotion fixture already joined")?.join().map_err(|_|"promotion fixture panicked".to_string())?;
            for result in clients{result?;}Ok(())
        }
    }
    impl Drop for Fixture {fn drop(&mut self){if self.join.is_some(){let _=self.finish();}}}

    fn serve(stream:TcpStream,stop:Arc<AtomicBool>,active:Arc<Mutex<Vec<TcpStream>>>)->std::result::Result<(),String> {
        stream.set_read_timeout(Some(Duration::from_millis(250))).map_err(|error|error.to_string())?;
        stream.set_write_timeout(Some(Duration::from_millis(250))).map_err(|error|error.to_string())?;
        active.lock().unwrap().push(stream.try_clone().map_err(|error|error.to_string())?);
        let mut socket=tungstenite::accept(stream).map_err(|error|error.to_string())?;
        let request=socket.read().map_err(|error|error.to_string())?.into_text().map_err(|error|error.to_string())?;
        let request:Value=serde_json::from_str(&request).map_err(|error|error.to_string())?;
        if request["method"]!="SUBSCRIBE"||request["params"]!=json!(["btcusdt@bookTicker"]){return Err("promotion fixture expected actual subscription".into())}
        let generation=request["id"].as_u64().ok_or("promotion fixture missing generation")?;
        socket.send(Message::Text(json!({"id":generation,"result":null}).to_string().into())).map_err(|error|error.to_string())?;
        let until=Instant::now()+FIXTURE_HORIZON;
        while !stop.load(Ordering::Acquire)&&Instant::now()<until{thread::sleep(Duration::from_millis(5));}
        let _=socket.get_mut().shutdown(Shutdown::Both);Ok(())
    }

    fn await_connected(runtime:&mut Runtime,count:usize) {
        let until=Instant::now()+ACK_LIMIT;
        while runtime.workers.len()!=count||runtime.workers.values().any(|worker|!worker.connected) {
            assert!(Instant::now()<until,"actual runtime ACK qualification exceeded fixture limit");
            runtime.recv(Duration::from_millis(10)).unwrap();
        }
        assert!(runtime.workers.values().all(|worker|worker.join.is_some()));
    }

    #[test]
    fn foundation_promotion_disconnect_before_old_stop_preserves_healthy_owner() {
        let mut fixture=Fixture::start();
        // Actual start/rotate own every worker, socket, JoinHandle and stop bit.
        let mut runtime=Runtime::start(fixture.settings()).unwrap();await_connected(&mut runtime,2);
        let old=runtime.workers.iter().find(|(_,worker)|!worker.spot).unwrap().0.clone();
        let old_stop=runtime.workers[&old].stop.clone();
        runtime.rotate(0).unwrap();await_connected(&mut runtime,3);
        let replacement=runtime.workers.iter().find(|(_,worker)|worker.replacement).unwrap().0.clone();
        let control=runtime.control();
        let (reached,observed)=mpsc::sync_channel(1);let (resume,resumed)=mpsc::sync_channel(1);
        *PAUSE.lock().unwrap()=Some(Pause{physical:control.physical.clone(),route:replacement.clone(),reached,resume:resumed});
        let _registration=PauseRegistration(control.physical.clone());
        let physical=control.physical.clone();let route=replacement.clone();let stop=old_stop.clone();
        let mut mutation=MutationJoin(Some(thread::spawn(move||{
            observed.recv_timeout(RACE_LIMIT).map_err(|_|"promotion never reached pre-stop pause".to_string())?;
            let already_stopped=stop.load(Ordering::Acquire);
            physical.lock().map_err(|_|"promotion physical lock poisoned")?.get_mut(&route).ok_or("missing actual replacement")?["connected"]=json!(false);
            resume.send(()).map_err(|_|"promotion pause disappeared".to_string())?;Ok(already_stopped)
        })));
        // Explicit unit-caller feedback only: these ACK-qualified sockets supply
        // no quote/selector qualification evidence and no fabricated reply.
        *control.promotion.lock().unwrap()=Some(replacement.clone());
        let rows=runtime.recv(Duration::ZERO);
        let stopped_before_disconnect=mutation.finish().unwrap();
        let old_present=runtime.workers.contains_key(&old);let old_stopped=old_stop.load(Ordering::Acquire);
        let truth=control.physical.lock().unwrap().clone();
        let still_replacement=runtime.workers[&replacement].replacement;
        runtime.shutdown().unwrap();fixture.finish().unwrap();
        assert!(!stopped_before_disconnect,"fixture disconnected only after old.stop");
        assert_eq!(rows,Ok(vec![]));
        assert!(old_present&&!old_stopped&&truth.get(&old).is_some_and(|state|state["connected"]==true)&&still_replacement,
            "pre-stop replacement disconnect must retain healthy old owner: old_present={old_present}, old_stopped={old_stopped}, old_truth={:?}, still_replacement={still_replacement}, rows={rows:?}",truth.get(&old));
    }

    pub(super) struct StopProbe {
        physical:Arc<Mutex<BTreeMap<String,Value>>>,stop:Arc<AtomicBool>,guarded_address:usize,
        entered:SyncSender<()>,blocked:Receiver<()>,stored:SyncSender<()>,checked:Receiver<()>,
    }
    static STOP_PROBE:Mutex<Option<StopProbe>>=Mutex::new(None);

    fn guard_address(guard:&std::sync::MutexGuard<'_,BTreeMap<String,Value>>)->usize {
        &**guard as *const BTreeMap<String,Value> as usize
    }

    pub(super) fn begin_stop_commit(guard:&std::sync::MutexGuard<'_,BTreeMap<String,Value>>,stop:&AtomicBool)->Option<StopProbe> {
        let probe={
            let mut slot=STOP_PROBE.lock().unwrap_or_else(|error|error.into_inner());
            if slot.as_ref().is_some_and(|probe|probe.guarded_address==guard_address(guard)&&std::ptr::eq(probe.stop.as_ref(),stop)){slot.take()}else{None}
        };
        if let Some(probe)=probe.as_ref() {
            assert!(!stop.load(Ordering::Acquire),"actual stop site was reached after old.stop");
            probe.entered.send(()).expect("atomic contender disappeared before stop");
            probe.blocked.recv_timeout(RACE_LIMIT).expect("atomic contender did not probe before stop");
            assert!(!stop.load(Ordering::Acquire),"old.stop moved before the guarded store");
        }
        probe
    }

    pub(super) fn finish_stop_commit(guard:&std::sync::MutexGuard<'_,BTreeMap<String,Value>>,stop:&AtomicBool,probe:Option<StopProbe>) {
        if let Some(probe)=probe {
            assert_eq!(guard_address(guard),probe.guarded_address,"stop site changed physical guard");
            assert!(std::ptr::eq(probe.stop.as_ref(),stop),"stop site changed atomic owner");
            assert!(stop.load(Ordering::Acquire),"old.stop moved outside the guarded helper");
            probe.stored.send(()).expect("atomic contender disappeared after stop");
            probe.checked.recv_timeout(RACE_LIMIT).expect("atomic contender did not probe after stop");
        }
    }

    struct StopProbeRegistration {
        physical:Arc<Mutex<BTreeMap<String,Value>>>,stop:Arc<AtomicBool>,
    }
    impl Drop for StopProbeRegistration {
        fn drop(&mut self) {
            let mut slot=STOP_PROBE.lock().unwrap_or_else(|error|error.into_inner());
            if slot.as_ref().is_some_and(|probe|Arc::ptr_eq(&probe.physical,&self.physical)&&Arc::ptr_eq(&probe.stop,&self.stop)){slot.take();}
        }
    }

    #[derive(Debug)]
    struct StopObservation {
        blocked_before:bool,blocked_after:bool,stop_before:bool,stop_after:bool,stop_when_acquired:bool,
    }
    struct AtomicContender {
        physical:Arc<Mutex<BTreeMap<String,Value>>>,stop:Arc<AtomicBool>,route:String,
        entered:Receiver<()>,blocked:SyncSender<()>,stored:Receiver<()>,checked:SyncSender<()>,
    }
    struct AtomicContenderJoin(Option<JoinHandle<Result<StopObservation>>>);
    impl AtomicContenderJoin {
        fn finish(&mut self)->Result<StopObservation> {
            self.0.take().ok_or("atomic contender already joined")?.join().map_err(|_|"atomic contender panicked".to_string())?
        }
    }
    impl Drop for AtomicContenderJoin {fn drop(&mut self){if self.0.is_some(){let _=self.finish();}}}

    fn disconnect_after_commit(contender:&AtomicContender)->Result<bool> {
        let until=Instant::now()+RACE_LIMIT;
        while Instant::now()<until {
            match contender.physical.try_lock() {
                Ok(mut truth)=>{
                    let stopped=contender.stop.load(Ordering::Acquire);
                    truth.get_mut(&contender.route).ok_or("atomic contender lost actual replacement")?["connected"]=json!(false);
                    return Ok(stopped)
                },
                Err(std::sync::TryLockError::WouldBlock)=>thread::sleep(Duration::from_millis(1)),
                Err(std::sync::TryLockError::Poisoned(_))=>return Err("atomic contender physical lock poisoned".into()),
            }
        }
        Err("atomic contender could not acquire physical truth after commit".into())
    }

    fn contend_stop(contender:AtomicContender)->Result<StopObservation> {
        contender.entered.recv_timeout(RACE_LIMIT).map_err(|_|"actual stop-site probe not reached".to_string())?;
        let stop_before=contender.stop.load(Ordering::Acquire);
        let blocked_before=matches!(contender.physical.try_lock(),Err(std::sync::TryLockError::WouldBlock));
        contender.blocked.send(()).map_err(|_|"actual stop-site probe disappeared before store".to_string())?;
        contender.stored.recv_timeout(RACE_LIMIT).map_err(|_|"guarded atomic store not observed".to_string())?;
        let stop_after=contender.stop.load(Ordering::Acquire);
        let blocked_after=matches!(contender.physical.try_lock(),Err(std::sync::TryLockError::WouldBlock));
        contender.checked.send(()).map_err(|_|"actual stop-site probe disappeared after store".to_string())?;
        let stop_when_acquired=disconnect_after_commit(&contender)?;
        Ok(StopObservation{blocked_before,blocked_after,stop_before,stop_after,stop_when_acquired})
    }

    #[test]
    fn foundation_promotion_atomic_guard_owns_actual_stop_commit() {
        let mut fixture=Fixture::start();
        let mut runtime=Runtime::start(fixture.settings()).unwrap();await_connected(&mut runtime,2);
        let old=runtime.workers.iter().find(|(_,worker)|!worker.spot).unwrap().0.clone();
        let old_stop=runtime.workers[&old].stop.clone();
        runtime.rotate(0).unwrap();await_connected(&mut runtime,3);
        let replacement=runtime.workers.iter().find(|(_,worker)|worker.replacement).unwrap().0.clone();
        let control=runtime.control();
        let address=guard_address(&control.physical.lock().unwrap());
        let (entered,enter_observed)=mpsc::sync_channel(1);let (blocked,block_observed)=mpsc::sync_channel(1);
        let (stored,store_observed)=mpsc::sync_channel(1);let (checked,check_observed)=mpsc::sync_channel(1);
        *STOP_PROBE.lock().unwrap()=Some(StopProbe{physical:control.physical.clone(),stop:old_stop.clone(),guarded_address:address,entered,blocked:block_observed,stored,checked:check_observed});
        let _registration=StopProbeRegistration{physical:control.physical.clone(),stop:old_stop.clone()};
        let contender=AtomicContender{physical:control.physical.clone(),stop:old_stop.clone(),route:replacement.clone(),entered:enter_observed,blocked,stored:store_observed,checked};
        let mut contender=AtomicContenderJoin(Some(thread::spawn(move||contend_stop(contender))));
        // Actual ACK/socket owners; promotion feedback is unit caller input,
        // without quote/selector qualification or a fabricated publication.
        *control.promotion.lock().unwrap()=Some(replacement.clone());
        let rows=runtime.recv(Duration::ZERO);
        let observed=contender.finish().unwrap();
        let stopped=old_stop.load(Ordering::Acquire);let retired=!runtime.workers.contains_key(&old);
        let replacement_truth=control.physical.lock().unwrap().get(&replacement).unwrap().clone();
        runtime.shutdown().unwrap();fixture.finish().unwrap();
        assert!(rows.is_ok(),"actual promotion failed: {rows:?}");
        assert!(observed.blocked_before&&observed.blocked_after&&!observed.stop_before&&observed.stop_after&&observed.stop_when_acquired,
            "physical contender crossed actual guarded stop site: {observed:?}");
        assert!(stopped&&retired,"actual old worker stop/JoinHandle retirement did not finish");
        assert_eq!(replacement_truth["connected"],false,"post-commit disconnect must remain visible in actual truth");
    }
}
