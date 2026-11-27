//! Bounded native-owned physical workers. Overflow invalidates the entire owner.
//! The consumer must retire its selector/Engine on any runtime error.
use crate::{Clock, Result, CAPACITY, MAX_SYMBOLS};
pub mod handoff;
use handoff::{Slot,Targets,Plan};
use crate::transport::{PublicSocket, Reception, SpotInstruments, SpotQuote, SpotReception, SpotSocket, SpotVenue, TextDecode};
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
#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SpotSubscription {
    pub endpoint: Endpoint,
    pub instruments: SpotInstruments,
}

pub type SpotSubscriptions = BTreeMap<SpotVenue, SpotSubscription>;

enum Event {
    Connected { route:String, token:String, generation:u64 },
    Frame { route:String, token:String, generation:u64, payload:Value, clock:Clock },
    VenueQuote { route: String, token: String, generation: u64, quote: SpotQuote, clock: Clock, batch_has_more: bool },
    Disconnected { route:String, generation:u64 },
    CreditedConnected { route:String, token:String, generation:u64, credit:IngressPermit },
    CreditedFrame { route:String, token:String, generation:u64, payload:Value, clock:Clock, credit:IngressPermit },
    CreditedVenueQuote { route: String, token: String, generation: u64, quote: SpotQuote, clock: Clock, batch_has_more: bool, credit: IngressPermit },
    CreditedDisconnected { route:String, generation:u64, credit:IngressPermit },
}
#[derive(Default)]
struct WorkerExit { canceled_post_ack:usize }
struct Worker { stop:Arc<AtomicBool>, join:Option<JoinHandle<WorkerExit>>, slot:u8, spot:bool,
    venue: Option<SpotVenue>,
    replacement:bool, token:String, generation:u64, connected:bool, created_ms:u64,due_ms:u64,
    spot_ingress:Option<Arc<SpotIngress>> }
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
/// Monotonic capture times at three independent observation boundaries.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SpotIngressClocks {
    pub observed: Option<i64>,
    pub offered: Option<i64>,
    pub dequeued: Option<i64>,
}

struct SpotIngressSlot {
    observed: AtomicI64,
    offered: AtomicI64,
    dequeued: AtomicI64,
}

impl SpotIngressSlot {
    fn new() -> Self {
        Self { observed: AtomicI64::new(-1), offered: AtomicI64::new(-1), dequeued: AtomicI64::new(-1) }
    }

    fn read(&self) -> SpotIngressClocks {
        let read = |point: &AtomicI64| Some(point.load(Ordering::Acquire)).filter(|time| *time >= 0);
        SpotIngressClocks { observed: read(&self.observed), offered: read(&self.offered), dequeued: read(&self.dequeued) }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct SpotCreditWaitSummary {
    granted: u64,
    stopped: u64,
    failed: u64,
    completed_total_ns: u64,
    completed_max_ns: u64,
    in_flight_age_ns: Option<u64>,
}

/// Private report metadata for comparing adjacent reads of the same wait counters.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
pub struct SpotCreditInterval {
    worker_instance: u64,
    generation: u64,
    sampled_at_ns: u64,
}

// A single spot worker writes these fixed-size counters. An observer never
// locks the credit pool or waits for a concurrent transition to finish.
struct SpotCreditWait {
    origin: Instant,
    version: AtomicU64,
    started: AtomicU64,
    granted: AtomicU64,
    stopped: AtomicU64,
    failed: AtomicU64,
    total: AtomicU64,
    max: AtomicU64,
}

impl SpotCreditWait {
    const IDLE: u64 = u64::MAX;

    fn new(origin: Instant) -> Self {
        Self { origin, version: AtomicU64::new(0), started: AtomicU64::new(Self::IDLE),
            granted: AtomicU64::new(0), stopped: AtomicU64::new(0), failed: AtomicU64::new(0),
            total: AtomicU64::new(0), max: AtomicU64::new(0) }
    }

    fn now_ns(&self) -> u64 {
        self.origin.elapsed().as_nanos().min(u128::from(Self::IDLE - 1)) as u64
    }

    fn write(&self, update: impl FnOnce()) {
        let version = self.version.load(Ordering::SeqCst);
        // Exhaustion permanently makes this generation's summary unavailable.
        if version > u64::MAX - 2 {
            self.version.store(u64::MAX, Ordering::SeqCst);
            return;
        }
        self.version.store(version + 1, Ordering::SeqCst);
        update();
        self.version.store(version + 2, Ordering::SeqCst);
    }

    fn begin(&self) {
        self.write(|| self.started.store(self.now_ns(), Ordering::SeqCst));
    }

    fn finish(&self, outcome: OfferResult) {
        let end = self.now_ns();
        self.write(|| {
            let start = self.started.swap(Self::IDLE, Ordering::SeqCst);
            let duration = end.saturating_sub(start);
            let count = match outcome {
                OfferResult::Sent => &self.granted,
                OfferResult::Stopped => &self.stopped,
                OfferResult::Failed => &self.failed,
            };
            count.store(count.load(Ordering::SeqCst).saturating_add(1), Ordering::SeqCst);
            self.total.store(self.total.load(Ordering::SeqCst).saturating_add(duration), Ordering::SeqCst);
            self.max.fetch_max(duration, Ordering::SeqCst);
        });
    }

    fn snapshot(&self) -> Option<SpotCreditWaitSummary> {
        self.snapshot_with_time(false).map(|(summary, _)| summary)
    }

    fn snapshot_with_time(&self, timestamped: bool) -> Option<(SpotCreditWaitSummary, Option<u64>)> {
        let before = self.version.load(Ordering::SeqCst);
        if before % 2 != 0 { return None; }
        let started = self.started.load(Ordering::SeqCst);
        let mut summary = SpotCreditWaitSummary {
            granted: self.granted.load(Ordering::SeqCst),
            stopped: self.stopped.load(Ordering::SeqCst),
            failed: self.failed.load(Ordering::SeqCst),
            completed_total_ns: self.total.load(Ordering::SeqCst),
            completed_max_ns: self.max.load(Ordering::SeqCst),
            in_flight_age_ns: None,
        };
        // Use the same time for the sample and its active wait age. The old
        // selector still reads the clock only when a wait is active.
        let sampled_at_ns = (timestamped || started != Self::IDLE).then(|| self.now_ns());
        if started != Self::IDLE {
            summary.in_flight_age_ns = Some(sampled_at_ns?.checked_sub(started)?);
        }
        (self.version.load(Ordering::SeqCst) == before).then_some((summary, sampled_at_ns))
    }
}

#[derive(Clone, Copy, Serialize)]
pub struct SpotReceiveStages {
    worker_instance: u64,
    generation: u64,
    text_received_ns: u64,
    worker_observed_ms: i64,
    offered: bool,
    dequeued_at_ms: Option<i64>,
    decoded: u64,
    gaps_over_one_second: u64,
    max_text_gap_ns: u64,
}

#[derive(Default)]
struct SpotReceiveSlot {
    latest: Option<(u64, u64, i64)>, // FIFO ordinal, text nanoseconds, original worker milliseconds
    dequeued_at_ms: Option<i64>,
    decoded: u64,
    gaps_over_one_second: u64,
    max_text_gap_ns: u64,
}

// The worker offers in its read order, including its pre-ACK buffer. The
// runtime drains that same per-generation FIFO. Use all-frame ordinals to
// join the two measurements without widening Event or relying on unique u.
struct SpotReceiveState {
    slots: BTreeMap<String, SpotReceiveSlot>,
    received: u64,
    offered: u64,
    dequeued: u64,
}

impl SpotReceiveState {
    fn new(symbols: &[String]) -> Self {
        Self { slots: symbols.iter().map(|symbol| (symbol.clone(), SpotReceiveSlot::default())).collect(),
            received: 0, offered: 0, dequeued: 0 }
    }

    fn capture(&mut self, payload: &Value, text_ns: Option<u64>, observed_ms: i64) {
        if self.received == u64::MAX { return; }
        self.received += 1;
        let Some(slot) = payload.get("s").and_then(Value::as_str).and_then(|s| self.slots.get_mut(s)) else { return };
        let Some(text_ns) = text_ns else { slot.latest = None; return };
        if let Some((_, previous, _)) = slot.latest {
            let gap = text_ns.saturating_sub(previous);
            slot.gaps_over_one_second += u64::from(gap > 1_000_000_000);
            slot.max_text_gap_ns = slot.max_text_gap_ns.max(gap);
        }
        slot.latest = Some((self.received, text_ns, observed_ms));
        slot.dequeued_at_ms = None;
        slot.decoded = slot.decoded.saturating_add(1);
    }

    fn dequeue(&mut self, symbol: &str, processing_ms: i64) {
        self.dequeued = self.dequeued.saturating_add(1);
        if let Some(slot) = self.slots.get_mut(symbol) {
            if slot.latest.is_some_and(|(ordinal, _, _)| ordinal == self.dequeued) {
                slot.dequeued_at_ms = Some(processing_ms);
            }
        }
    }

    fn sample(&self, worker_instance: u64, generation: u64, symbol: &str) -> Option<SpotReceiveStages> {
        if self.received == u64::MAX { return None; }
        let slot = self.slots.get(symbol)?;
        let (ordinal, text_received_ns, worker_observed_ms) = slot.latest?;
        Some(SpotReceiveStages { worker_instance, generation, text_received_ns, worker_observed_ms,
            offered: ordinal <= self.offered.max(self.dequeued), dequeued_at_ms: slot.dequeued_at_ms,
            decoded: slot.decoded, gaps_over_one_second: slot.gaps_over_one_second,
            max_text_gap_ns: slot.max_text_gap_ns })
    }
}

#[derive(Clone, Copy)]
pub enum SpotProbe { Off, Ingress, ReceiveStages }

// One fixed subscription per spot connection generation. Slots are installed
// during ACK; only the original worker and consumer write atomics afterwards.
struct SpotIngress {
    token: String,
    worker_instance: u64,
    generation: u64,
    slots: BTreeMap<String, SpotIngressSlot>,
    credit_wait: Option<SpotCreditWait>,
    receive: Option<Mutex<SpotReceiveState>>,
}

impl SpotIngress {
    fn for_generation(
        token: &str,
        worker_instance: u64,
        generation: u64,
        symbols: &[String],
        credit_origin: Option<Instant>,
    ) -> Self {
        Self { token: token.into(), worker_instance, generation,
            slots: symbols.iter().map(|symbol| (symbol.clone(), SpotIngressSlot::new())).collect(),
            credit_wait: credit_origin.map(SpotCreditWait::new), receive: None }
    }

    fn matches(&self, token: &str, generation: u64) -> bool {
        self.token == token && self.generation == generation
    }

    fn slot(&self, payload: &Value) -> Option<&SpotIngressSlot> {
        self.slots.get(payload.get("s")?.as_str()?)
    }

    fn record_offer(&self) {
        if let Some(mut state) = self.receive.as_ref().and_then(|state| state.lock().ok()) {
            state.offered = state.offered.saturating_add(1);
        }
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
    spot_ingress: Option<Arc<Mutex<Option<Arc<SpotIngress>>>>>,
    spot_decode_failures: Option<Arc<AtomicU64>>,
}
impl Default for Control {fn default()->Self{Self{wake:WakeSignal::default(),physical:Arc::new(Mutex::new(BTreeMap::new())),promotion:Arc::new(Mutex::new(None)),origin:Instant::now(),fixture_wall:Arc::new(AtomicI64::new(0)),failure:Arc::new(Mutex::new(None)),failure_accounting:Arc::new(Mutex::new(None)),handoff:None,failure_progress:Arc::new(Mutex::new(None)),spot_ingress:None,spot_decode_failures:None}}}
impl Control {pub fn clock(&self)->Clock {let mut c=clock(self.origin);let wall=self.fixture_wall.load(Ordering::Acquire);if wall!=0{c.wall_ms=wall+c.monotonic_ms;}c}}
impl Control {
    fn spot_ingress_generation(&self, token: &str, generation: u64) -> Option<Arc<SpotIngress>> {
        let current = self.spot_ingress.as_ref()?.lock().ok()?;
        current.as_ref().filter(|ingress| ingress.matches(token, generation)).cloned()
    }

    pub fn spot_ingress_clocks(&self, token: &str, generation: u64, symbol: &str) -> Option<SpotIngressClocks> {
        let current = self.spot_ingress_generation(token, generation)?;
        Some(current.slots.get(symbol)?.read())
    }

    pub fn spot_receive_stages(&self, token: &str, generation: u64, symbol: &str) -> Option<SpotReceiveStages> {
        let current = self.spot_ingress.as_ref()?.try_lock().ok()?;
        let ingress = current.as_ref().filter(|ingress| ingress.matches(token, generation))?;
        let state = ingress.receive.as_ref()?.try_lock().ok()?;
        state.sample(ingress.worker_instance, generation, symbol)
    }

    pub fn spot_decode_failures(&self) -> Option<u64> {
        self.spot_decode_failures.as_ref().map(|count| count.load(Ordering::Relaxed))
    }

    pub fn spot_credit_wait(&self, token: &str, generation: u64) -> Option<SpotCreditWaitSummary> {
        self.read_spot_credit(token, generation, |_, wait| wait.snapshot())
    }

    pub fn spot_credit_interval(
        &self, token: &str, generation: u64,
    ) -> Option<(SpotCreditWaitSummary, SpotCreditInterval)> {
        self.read_spot_credit(token, generation, |ingress, wait| {
            let (summary, sampled_at_ns) = wait.snapshot_with_time(true)?;
            Some((summary, SpotCreditInterval {
                worker_instance: ingress.worker_instance,
                generation: ingress.generation,
                sampled_at_ns: sampled_at_ns.filter(|&value| value < SpotCreditWait::IDLE - 1)?,
            }))
        })
    }

    fn read_spot_credit<T>(
        &self,
        token: &str,
        generation: u64,
        read: impl FnOnce(&SpotIngress, &SpotCreditWait) -> Option<T>,
    ) -> Option<T> {
        // The caller holds physical truth for the actor's acknowledged route.
        // Retain the generation guard throughout the diagnostic read.
        let current = self.spot_ingress.as_ref()?.try_lock().ok()?;
        let ingress = current.as_ref().filter(|ingress| ingress.matches(token, generation))?;
        read(ingress, ingress.credit_wait.as_ref()?)
    }
}
pub struct Runtime {
    settings:Settings, workers:BTreeMap<String,Worker>, sender:SyncSender<Event>, receiver:Receiver<Event>,
    native_spot: SpotSubscriptions,
    native_spot_generation: Arc<AtomicU64>,
    failure:Arc<Mutex<Option<String>>>, origin:Instant, next:u64, generations:BTreeMap<String,u64>,
    memberships:BTreeMap<String,u64>, serials:BTreeMap<String,u64>, supplied:u64, retired:u64,
    fixture_wall:Arc<AtomicI64>, anchored:bool,
    received:Arc<AtomicU64>, retired_frames:u64, queue_age:AgeStats, source_age:AgeStats,
    control:Control,
    recv_total:Timing, channel_wait:Timing,
    lifecycle:LifecyclePolicy,
    retiring:BTreeMap<String,Worker>, pending_targets:Option<Arc<Plan>>,
    ingress_mode:IngressMode, ingress_credits:Option<Arc<IngressCredits>>,
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
// Legacy exact v1 selector; Runtime captures the selected mode once.
fn ingress_cooperate(value:Option<&std::ffi::OsStr>)->bool {
    value.and_then(std::ffi::OsStr::to_str)==Some("v1")
}
fn yield_raw_ingress(cooperate:bool) {if cooperate {std::thread::yield_now();}}
// Original admission, returned-payload release and wake finish before scheduling.
fn emit_cooperatively<T,F:FnOnce()>(sender:&SyncSender<T>,failure:&Mutex<Option<String>>,wake:&WakeSignal,event:T,schedule:F)->bool {
    let sent=emit_notified(sender,failure,wake,event);if sent {schedule();}sent
}

#[derive(Clone,Copy,Debug,PartialEq,Eq)]
enum IngressMode { Off, V1, V2 }
fn select_ingress_mode(value:Option<&std::ffi::OsStr>,configured:bool)->Result<IngressMode> {
    if value.and_then(std::ffi::OsStr::to_str)==Some("v2") {
        if !configured{return Err("native ingress v2 requires configured whole-plan runtime".into())}
        return Ok(IngressMode::V2)
    }
    Ok(if ingress_cooperate(value){IngressMode::V1}else{IngressMode::Off})
}
struct CreditState {free:usize,#[cfg(test)] waiting:usize,#[cfg(test)] parks:u64}
struct IngressCredits {
    state:Mutex<CreditState>, ready:Condvar,
    #[cfg(test)] captures:Mutex<Vec<(u64,Clock)>>,
    #[cfg(test)] before_receive_wait:Mutex<Option<SyncSender<()>>>,
}
impl IngressCredits {
    fn new()->Arc<Self> {Arc::new(Self{state:Mutex::new(CreditState{free:CAPACITY,#[cfg(test)] waiting:0,#[cfg(test)] parks:0}),ready:Condvar::new(),#[cfg(test)] captures:Mutex::new(Vec::new()),#[cfg(test)] before_receive_wait:Mutex::new(None)})}
    fn reserve(self:&Arc<Self>,stop:&AtomicBool,failure:&Mutex<Option<String>>,observation:Option<&SpotCreditWait>)->std::result::Result<IngressPermit,OfferResult> {
        let mut episode = None;
        let outcome = (|| loop {
            check_offer(stop,failure)?;
            let mut state=self.state.lock().map_err(|_|OfferResult::Failed)?;
            if state.free>0 {state.free-=1;return Ok(IngressPermit{pool:self.clone()})}
            #[cfg(test)] {state.waiting+=1;state.parks+=1;self.ready.notify_all();}
            if episode.is_none() {
                if let Some(wait) = observation {
                    wait.begin();
                    episode = Some(wait);
                }
            }
            // The failure/physical locks are never held while this wait parks.
            state=self.ready.wait_timeout(state,Duration::from_millis(20)).map_err(|_|OfferResult::Failed)?.0;
            #[cfg(test)] {state.waiting-=1;}
            drop(state);
        })();
        if let Some(wait) = episode {
            wait.finish(outcome.as_ref().map_or_else(|result| *result, |_| OfferResult::Sent));
        }
        outcome
    }
    #[cfg(test)]
    fn observe_capture(&self,payload:&Value,clock:Clock) {
        let mut captures=self.captures.lock().unwrap();
        if captures.len()<CAPACITY*2 {captures.push((payload["u"].as_u64().unwrap_or(0),clock));}
    }
}
// A permit can only return to its originating Runtime, exactly once on drop.
struct IngressPermit {pool:Arc<IngressCredits>}
impl Drop for IngressPermit {
    fn drop(&mut self) {
        let mut state=self.pool.state.lock().unwrap_or_else(|error|error.into_inner());
        debug_assert!(state.free<CAPACITY);state.free+=1;
        drop(state);self.pool.ready.notify_one();
    }
}
impl Event {
    fn with_credit(self,credit:IngressPermit)->Self {
        match self {
            Self::Connected{route,token,generation}=>Self::CreditedConnected{route,token,generation,credit},
            Self::Frame{route,token,generation,payload,clock}=>Self::CreditedFrame{route,token,generation,payload,clock,credit},
            Self::VenueQuote{route,token,generation,quote,clock,batch_has_more}=>Self::CreditedVenueQuote{route,token,generation,quote,clock,batch_has_more,credit},
            Self::Disconnected{route,generation}=>Self::CreditedDisconnected{route,generation,credit},
            _=>unreachable!("an ingress Event is credited only once"),
        }
    }
    fn release_credit(self)->Self {
        match self {
            Self::CreditedConnected{route,token,generation,credit}=>{drop(credit);Self::Connected{route,token,generation}},
            Self::CreditedFrame{route,token,generation,payload,clock,credit}=>{drop(credit);Self::Frame{route,token,generation,payload,clock}},
            Self::CreditedVenueQuote{route,token,generation,quote,clock,batch_has_more,credit}=>{drop(credit);Self::VenueQuote{route,token,generation,quote,clock,batch_has_more}},
            Self::CreditedDisconnected{route,generation,credit}=>{drop(credit);Self::Disconnected{route,generation}},
            event=>event,
        }
    }
    fn is_quote(&self)->bool {matches!(self, Self::Frame{..} | Self::VenueQuote{..})}
    #[cfg(test)]
    fn has_credit(&self)->bool {matches!(self,Self::CreditedConnected{..}|Self::CreditedFrame{..}|Self::CreditedVenueQuote{..}|Self::CreditedDisconnected{..})}
}
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
enum OfferResult {Sent,Stopped,Failed}
fn check_offer(stop:&AtomicBool,failure:&Mutex<Option<String>>)->std::result::Result<(),OfferResult> {
    // Failure wins over intentional cancellation, including a simultaneous stop.
    if failure.lock().map_or(true,|first|first.is_some()){return Err(OfferResult::Failed)}
    if stop.load(Ordering::Acquire){return Err(OfferResult::Stopped)}Ok(())
}
struct WorkerIngress {mode:IngressMode,credits:Option<Arc<IngressCredits>>,stop:Arc<AtomicBool>}
impl WorkerIngress {
    fn offer(&self,sender:&SyncSender<Event>,failure:&Mutex<Option<String>>,wake:&WakeSignal,event:Event)->OfferResult {
        self.offer_observed(sender, failure, wake, event, None)
    }

    fn offer_observed(&self,sender:&SyncSender<Event>,failure:&Mutex<Option<String>>,wake:&WakeSignal,mut event:Event,observation:Option<&SpotCreditWait>)->OfferResult {
        if let Some(credits)=&self.credits {
            let permit=match credits.reserve(&self.stop,failure,observation) {
                Ok(permit)=>permit,
                Err(result)=>{if result==OfferResult::Failed{fail_notified(failure,wake,"native ingress credit acquisition failed; owner incomplete".into());}return result},
            };
            if let Err(result)=check_offer(&self.stop,failure){return result}
            event=event.with_credit(permit);
            return if emit_notified(sender,failure,wake,event){OfferResult::Sent}else{OfferResult::Failed}
        }
        if matches!(event, Event::VenueQuote { .. }) {
            if let Err(result)=check_offer(&self.stop,failure){return result}
        }
        let raw=event.is_quote();
        let sent=if raw{emit_cooperatively(sender,failure,wake,event,||yield_raw_ingress(self.mode==IngressMode::V1))}else{emit_notified(sender,failure,wake,event)};
        if sent{OfferResult::Sent}else{OfferResult::Failed}
    }
}

fn offer_venue_batch<T>(
    quotes: Vec<T>, received: &AtomicU64, mut offer: impl FnMut(T, bool) -> OfferResult,
) -> Result<usize> {
    let captured = quotes.len();
    received.fetch_add(captured as u64, Ordering::Relaxed);
    for (index, quote) in quotes.into_iter().enumerate() {
        match offer(quote, index + 1 < captured) {
            OfferResult::Sent => {},
            OfferResult::Stopped => return Ok(captured - index),
            OfferResult::Failed => return Err("ingress failed".into()),
        }
    }
    Ok(0)
}

fn buffered_quote_is_old(prior: &SpotQuote, incoming: &SpotQuote) -> bool {
    if let (Some(prior), Some(incoming)) = (prior.sequence, incoming.sequence) {
        return incoming <= prior;
    }
    let equal_price = |left: &str, right: &str| {
        use crate::decimal::PositiveDecimal;
        left == right || PositiveDecimal::parse(left).zip(PositiveDecimal::parse(right))
            .is_some_and(|(left, right)| left.compare(&right).is_eq())
    };
    incoming.source_ms < prior.source_ms
        || (incoming.source_ms == prior.source_ms
            && equal_price(&prior.bid, &incoming.bid) && equal_price(&prior.ask, &incoming.ask))
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
fn extract_worker_payload(event:&mut Value)->Value {event.get_mut("payload").map(Value::take).unwrap_or(Value::Null)}

enum WorkerSocket {
    Binance(PublicSocket),
    Venue(SpotSocket),
}
impl WorkerSocket {
    fn close(self) -> Result<()> {
        match self {
            Self::Binance(socket) => socket.close(),
            Self::Venue(socket) => socket.close(),
        }
    }
}

fn capture_clock(origin: Instant, fixture_wall: &AtomicI64) -> Clock {
    let mut captured = clock(origin);
    let wall = fixture_wall.load(Ordering::Acquire);
    if wall != 0 { captured.wall_ms = wall + captured.monotonic_ms; }
    captured
}

fn venue_current(control: &Control, route: &str, token: &str, generation: u64) -> Result<bool> {
    let truth = control.physical.lock().map_err(|_| "physical truth poisoned")?;
    Ok(truth.get(route).is_some_and(|state| {
        state["connected"] == true && state["token"] == token && state["generation"] == generation
    }))
}

fn spot_route_row(worker: &Worker, connected: bool, bn_symbols: &[String], native_spot: &SpotSubscriptions) -> Value {
    if let Some(venue) = worker.venue {
        let symbols: Vec<_> = native_spot[&venue].instruments.keys().collect();
        json!({"type":"native_spot_route","venue":venue,"token":worker.token,"generation":worker.generation,"connected":connected,"symbols":symbols})
    } else {
        json!({"type":"spot_route","token":worker.token,"generation":worker.generation.max(1),"connected":connected,"symbols":bn_symbols})
    }
}
impl Runtime {
    pub fn start(settings:Settings)->Result<Self> {
        Self::start_with_policy(settings,LifecyclePolicy::default())
    }
    pub fn start_configured(settings:Settings)->Result<Self>{Self::start_with_policy_and_handoff(settings,LifecyclePolicy::default(),true)}
    fn start_with_policy(settings:Settings,lifecycle:LifecyclePolicy)->Result<Self>{Self::start_with_policy_and_handoff(settings,lifecycle,false)}
    fn start_with_policy_and_handoff(settings:Settings,lifecycle:LifecyclePolicy,configured:bool)->Result<Self>{
        let mode=select_ingress_mode(std::env::var_os("CEA_NATIVE_INGRESS_COOPERATE").as_deref(),configured)?;
        Self::start_with_ingress_mode(settings,lifecycle,configured,mode)
    }
    pub fn start_configured_with_spot_probe(settings: Settings) -> Result<Self> {
        let mode = select_ingress_mode(std::env::var_os("CEA_NATIVE_INGRESS_COOPERATE").as_deref(), true)?;
        Self::start_with_modes(settings, LifecyclePolicy::default(), true, mode, SpotProbe::Ingress)
    }

    pub fn start_configured_with_spot_receive_probe(settings: Settings) -> Result<Self> {
        let mode = select_ingress_mode(std::env::var_os("CEA_NATIVE_INGRESS_COOPERATE").as_deref(), true)?;
        Self::start_with_modes(settings, LifecyclePolicy::default(), true, mode, SpotProbe::ReceiveStages)
    }

    pub fn start_configured_with_native_spot(settings: Settings, native_spot: SpotSubscriptions) -> Result<Self> {
        Self::start_configured_with_native_spot_probe(settings, native_spot, SpotProbe::Off)
    }

    /// Install the optional ingress probe on the original Binance spot worker.
    pub fn start_configured_with_native_spot_probe(settings: Settings, native_spot: SpotSubscriptions, probe: SpotProbe) -> Result<Self> {
        let mode = select_ingress_mode(std::env::var_os("CEA_NATIVE_INGRESS_COOPERATE").as_deref(), true)?;
        Self::start_with_subscriptions(settings, LifecyclePolicy::default(), true, mode, probe, native_spot)
    }

    fn start_with_ingress_mode(settings:Settings,lifecycle:LifecyclePolicy,configured:bool,ingress_mode:IngressMode)->Result<Self>{
        Self::start_with_modes(settings, lifecycle, configured, ingress_mode, SpotProbe::Off)
    }

    fn start_with_modes(settings: Settings, lifecycle: LifecyclePolicy, configured: bool,
                        ingress_mode: IngressMode, spot_probe: SpotProbe) -> Result<Self> {
        Self::start_with_subscriptions(settings, lifecycle, configured, ingress_mode, spot_probe, BTreeMap::new())
    }

    fn start_with_subscriptions(settings: Settings, lifecycle: LifecyclePolicy, configured: bool,
                                ingress_mode: IngressMode, spot_probe: SpotProbe, native_spot: SpotSubscriptions) -> Result<Self> {
        if ingress_mode==IngressMode::V2&&!configured{return Err("native ingress v2 requires configured whole-plan runtime".into())}
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
        for (&venue, subscription) in &native_spot {
            let endpoint = &subscription.endpoint;
            SpotSocket::validate(venue, endpoint.address, &endpoint.uri, &subscription.instruments)?;
            if settings.fixture_wall_ms.is_some() && !endpoint.address.ip().is_loopback() {
                return Err("fixture clock requires loopback endpoints".into());
            }
            if subscription.instruments.keys().any(|symbol| !settings.perpetual_symbols.contains(symbol)) {
                return Err("native spot target lacks perpetual binding".into());
            }
        }
        let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        let memberships=settings.perpetual_symbols.iter().map(|s|(s.clone(),1)).collect();
        let fixture_wall=Arc::new(AtomicI64::new(settings.fixture_wall_ms.unwrap_or(0)));
        let mut control=Control::default();control.fixture_wall=fixture_wall.clone();
        if !matches!(spot_probe, SpotProbe::Off) {
            control.spot_ingress = Some(Arc::new(Mutex::new(None)));
        }
        if matches!(spot_probe, SpotProbe::ReceiveStages) {
            control.spot_decode_failures = Some(Arc::new(AtomicU64::new(0)));
        }
        let mut this=Self {settings,workers:BTreeMap::new(),sender,receiver,native_spot,native_spot_generation:Arc::new(AtomicU64::new(0)),failure:control.failure.clone(),origin:control.origin,next:0,generations:BTreeMap::new(),memberships,serials:BTreeMap::new(),supplied:0,retired:0,fixture_wall,anchored:false,received:Arc::new(AtomicU64::new(0)),retired_frames:0,queue_age:AgeStats::default(),source_age:AgeStats::default(),control,recv_total:Timing::default(),channel_wait:Timing::default(),lifecycle,retiring:BTreeMap::new(),pending_targets:None,ingress_mode,ingress_credits:if ingress_mode==IngressMode::V2{Some(IngressCredits::new())}else{None}};
        this.spawn(0,false,false)?;
        if this.settings.mode!="single" {this.spawn(1,false,false)?;}
        if configured{this.enable_handoff()?;}
        if starts_spot{this.spawn(0,true,false)?;}
        for venue in this.native_spot.iter().filter_map(|(&venue, sub)| (!sub.instruments.is_empty()).then_some(venue)).collect::<Vec<_>>() {
            this.spawn_worker(0, true, false, Some(venue))?;
        }
        Ok(this)
    }
    fn spawn(&mut self,slot:u8,spot:bool,replacement:bool)->Result<()> {
        self.spawn_worker(slot, spot, replacement, None)
    }
    fn spawn_worker(&mut self,slot:u8,spot:bool,replacement:bool,venue:Option<SpotVenue>)->Result<()> {
        if self.workers.values().chain(self.retiring.values()).filter(|w|w.spot==spot && w.venue==venue).count()>=if spot{1}else{3}{return Err("native physical slot capacity".into())}
        self.next+=1;let route=format!("{}-{}-{}",if spot{"spot"}else{"perp"},slot,self.next);
        let worker_instance = self.next;
        let created_ms=self.origin.elapsed().as_millis() as u64;let due_ms=self.lifecycle.rotation.initial_due(created_ms,slot);
        let subscription = venue.map(|venue| self.native_spot[&venue].clone());
        let endpoint=if let Some(sub)=&subscription {sub.endpoint.clone()}else if spot{self.settings.spot.clone()}else if slot==1{self.settings.fixture_backup_endpoint.clone().unwrap_or_else(||self.settings.perpetual.clone())}else{self.settings.perpetual.clone()};
        let symbols=if let Some(sub)=&subscription {sub.instruments.keys().cloned().collect()}else if spot{self.settings.spot_symbols.clone()}else{self.settings.perpetual_symbols.clone()};
        let native_generation = self.native_spot_generation.clone();
        let stop=Arc::new(AtomicBool::new(false));let worker_stop=stop.clone();let failure=self.failure.clone();let sender=self.sender.clone();
        let origin=self.origin;let fixture_wall=self.fixture_wall.clone();let received=self.received.clone();let id=route.clone();let session=self.settings.session.clone();
        let physical=self.control.physical.clone();let wake=self.control.wake.clone();
        let spot_observer = self.control.spot_ingress.as_ref().filter(|_| spot && venue.is_none()).cloned();
        let spot_decode_failures = self.control.spot_decode_failures.as_ref().filter(|_| spot && venue.is_none()).cloned();
        let mut initial = json!({"type":"route","route":route,"slot":slot,"spot":spot,"token":"pending","generation":1,"connected":false,"replacement":replacement,"applied":[]});
        if let Some(venue) = venue { initial["venue"] = json!(venue); }
        physical.lock().map_err(|_|"physical truth poisoned")?.insert(route.clone(), initial);
        let recovery=self.lifecycle.recovery;
        let ingress=WorkerIngress{mode:self.ingress_mode,credits:self.ingress_credits.clone(),stop:worker_stop.clone()};
        let join=thread::Builder::new().name(route.clone()).spawn(move||{
            let mut generation=0;let mut recovery=Recovery::new(recovery);let mut exit=WorkerExit::default();
            while !worker_stop.load(Ordering::Acquire) {
                if failure.lock().map_or(true,|f|f.is_some()){break}
                if venue.is_some() {
                    let previous = native_generation.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |value| value.checked_add(1));
                    let Ok(previous) = previous else {fail_notified(&failure,&wake,"native spot generation exhausted; owner incomplete".into());break};
                    generation = previous + 1;
                } else { generation += 1; }
                let token=format!("{session}-{id}-{generation}");
                let spot_ingress = spot_observer.as_ref().map(|_| {
                    let mut observation = SpotIngress::for_generation(&token, worker_instance, generation,
                        &symbols, ingress.credits.as_ref().map(|_| origin));
                    if spot_decode_failures.is_some() {
                        observation.receive = Some(Mutex::new(SpotReceiveState::new(&symbols)));
                    }
                    Arc::new(observation)
                });
                let credit_wait = spot_ingress.as_ref().and_then(|probe| probe.credit_wait.as_ref());
                let offer_route_event = |event, observation| {
                    // Off/v1 use only immediate bounded offers. Serialize the
                    // native control send with detach; v2 reserves outside truth.
                    let _truth = if venue.is_some() && ingress.credits.is_none() {
                        match physical.lock() {
                            Ok(truth) => Some(truth),
                            Err(_) => {
                                fail_notified(&failure, &wake, "physical truth poisoned".into());
                                return OfferResult::Failed;
                            }
                        }
                    } else { None };
                    if venue.is_some() {
                        if let Err(result) = check_offer(&worker_stop, &failure) { return result; }
                    }
                    ingress.offer_observed(&sender, &failure, &wake, event, observation)
                };
                let mut unqualified=0usize;
                let attempt=(||->Result<()> {
                    let mut socket = if let (Some(venue), Some(subscription)) = (venue, &subscription) {
                        let socket = SpotSocket::connect(venue, endpoint.address, &endpoint.uri, &subscription.instruments)?;
                        recovery.opened(origin.elapsed().as_millis() as u64);
                        WorkerSocket::Venue(socket)
                    } else {
                        let mut socket = PublicSocket::connect(endpoint.address, &endpoint.uri)?;
                        recovery.opened(origin.elapsed().as_millis() as u64);
                        socket.subscribe(generation, &symbols)?;
                        WorkerSocket::Binance(socket)
                    };
                    let ack_deadline=Instant::now()+Duration::from_secs(5);
                    let mut ack=false;let mut before_ack=Vec::new();
                    // Only individually ACKed native quotes enter this buffer.
                    // Replaced or unqualified captures never enter ingress accounting.
                    let mut before_venue_ack: Vec<(SpotQuote, Clock)> = Vec::new();
                    let offer_venue_quote = |quote, clock, batch_has_more| {
                        ingress.offer(&sender, &failure, &wake, Event::VenueQuote {
                            route:id.clone(),token:token.clone(),generation,quote,clock,batch_has_more,
                        })
                    };
                    while !worker_stop.load(Ordering::Acquire) {
                        if !ack&&Instant::now()>ack_deadline{return Err("subscription ACK deadline".into())}
                        let mut text_received_ns = None;
                        let received_frame = match &mut socket {
                            WorkerSocket::Venue(socket) => {
                                let mut captured = None;
                                match socket.receive(|| {
                                    let time = capture_clock(origin, &fixture_wall);
                                    captured = Some(time);
                                    time.wall_ms
                                })? {
                                    SpotReception::Idle => continue,
                                    SpotReception::Acknowledged => Ok(Reception::Event(json!({"type":"ack"}))),
                                    SpotReception::Quotes(quotes) => {
                                        let clock = captured.ok_or("native spot frame missing receive time")?;
                                        if !ack {
                                            for quote in quotes {
                                                if let Some(index) = before_venue_ack.iter().position(|(prior, _)| prior.symbol == quote.symbol) {
                                                    if buffered_quote_is_old(&before_venue_ack[index].0, &quote) {
                                                        continue;
                                                    }
                                                    before_venue_ack.remove(index);
                                                }
                                                if before_venue_ack.len() == MAX_SYMBOLS {
                                                    return Err("native spot pre-ACK symbol capacity".into());
                                                }
                                                before_venue_ack.push((quote, clock));
                                            }
                                            continue;
                                        }
                                        exit.canceled_post_ack = offer_venue_batch(quotes, &received,
                                            |quote, has_more| offer_venue_quote(quote, clock, venue == Some(SpotVenue::Okx) && has_more))?;
                                        if exit.canceled_post_ack != 0 {
                                            return Ok(());
                                        }
                                        continue;
                                    }
                                }
                            }
                            WorkerSocket::Binance(socket) => if let Some(failures) = &spot_decode_failures {
                                socket.receive_timeout_observed(|phase| match phase {
                                    TextDecode::Received => text_received_ns = u64::try_from(origin.elapsed().as_nanos()).ok(),
                                    TextDecode::Failed => { failures.fetch_add(1, Ordering::Relaxed); }
                                })
                            } else {
                                socket.receive_timeout()
                            }
                        }?;
                        match received_frame {
                            Reception::Idle=>continue,
                            Reception::Event(mut event)=>match event["type"].as_str() {
                                Some("ack")=>{
                                    ack=true;
                                    {let mut states=physical.lock().map_err(|_|"physical truth poisoned")?;let state=states.get_mut(&id).ok_or("retired physical owner")?;state["token"]=json!(token);state["generation"]=json!(generation);state["connected"]=json!(true);state["applied"]=json!(symbols);
                                        if let Some(mut observer) = spot_observer.as_ref().and_then(|observer| observer.lock().ok()) {
                                            *observer = spot_ingress.clone();
                                        }
                                    }
                                    match offer_route_event(Event::Connected{route:id.clone(),token:token.clone(),generation}, credit_wait) {
                                        OfferResult::Sent => {},
                                        OfferResult::Stopped if venue.is_some() => return Ok(()),
                                        _ => return Err("ingress failed".into()),
                                    }
                                    if !before_venue_ack.is_empty() {
                                        exit.canceled_post_ack = offer_venue_batch(
                                            std::mem::take(&mut before_venue_ack), &received,
                                            |(quote, clock), _| offer_venue_quote(quote, clock, false),
                                        )?;
                                        if exit.canceled_post_ack != 0 {
                                            return Ok(());
                                        }
                                    }
                                    for (payload,clock) in before_ack.drain(..) {
                                        let point = spot_ingress.as_ref().and_then(|probe| probe.slot(&payload));
                                        if ingress.offer_observed(&sender,&failure,&wake,Event::Frame{route:id.clone(),token:token.clone(),generation,payload,clock},credit_wait)!=OfferResult::Sent{return Err("ingress failed".into())}
                                        if let Some(point) = point {point.offered.store(clock.monotonic_ms, Ordering::Release);}
                                        if let Some(probe) = &spot_ingress { probe.record_offer(); }
                                        unqualified-=1;
                                    }
                                },
                                Some("raw")=>{
                                    received.fetch_add(1,Ordering::Relaxed);
                                    let payload=extract_worker_payload(&mut event);let clock=capture_clock(origin,&fixture_wall);
                                    #[cfg(test)]
                                    if let Some(credits)=&ingress.credits{credits.observe_capture(&payload,clock);}
                                    let point = spot_ingress.as_ref().and_then(|probe| probe.slot(&payload));
                                    if let Some(point) = point {point.observed.store(clock.monotonic_ms, Ordering::Release);}
                                    if let Some(mut stages) = spot_ingress.as_ref()
                                        .and_then(|probe| probe.receive.as_ref()).and_then(|state| state.lock().ok()) {
                                        stages.capture(&payload, text_received_ns, clock.monotonic_ms);
                                    }
                                    if !ack {if before_ack.len()>=CAPACITY{return Err("pre-ACK buffer full".into())}before_ack.push((payload,clock));unqualified+=1;}
                                    else {
                                        match ingress.offer_observed(&sender,&failure,&wake,Event::Frame{route:id.clone(),token:token.clone(),generation,payload,clock},credit_wait) {
                                            OfferResult::Sent=>{
                                                if let Some(point) = point {point.offered.store(clock.monotonic_ms, Ordering::Release);}
                                                if let Some(probe) = &spot_ingress { probe.record_offer(); }
                                            },
                                            OfferResult::Stopped=>{exit.canceled_post_ack=1;return Ok(())},
                                            OfferResult::Failed=>return Err("ingress failed".into()),
                                        }
                                    }
                                },
                                Some("ping"|"pong")=>{},_=>return Err("unknown physical frame".into()),
                            }
                        }
                    }
                    let _=socket.close();Ok(())
                })();
                if let Ok(mut states)=physical.lock(){if let Some(state)=states.get_mut(&id){state["connected"]=json!(false);state["applied"]=json!([]);}}
                if let Some(mut observer) = spot_observer.as_ref().and_then(|observer| observer.lock().ok()) {
                    if observer.as_ref().is_some_and(|probe| probe.matches(&token, generation)) {
                        *observer = None;
                    }
                }
                if unqualified>0 {fail_notified(&failure,&wake,format!("retired {unqualified} pre-ACK frames; owner incomplete"));}
                if offer_route_event(Event::Disconnected{route:id.clone(),generation},None)!=OfferResult::Sent {break}
                if worker_stop.load(Ordering::Acquire)||failure.lock().map_or(true,|f|f.is_some()){break}
                // Subscription or parse uncertainty cannot resume the same physical generation.
                let _=attempt;
                let jitter=match public_jitter(recovery.policy.spread()){Ok(jitter)=>jitter,Err(error)=>{fail_notified(&failure,&wake,error);break}};
                let delay=recovery.retry_delay(origin.elapsed().as_millis() as u64,jitter);
                if !retry_wait(&worker_stop,&failure,delay){break}
            }
            exit
        }).map_err(|e|e.to_string())?;
        self.serials.insert(route.clone(),0);
        self.workers.insert(route,Worker {stop,join:Some(join),slot,spot,venue,replacement,token:String::new(),generation:0,connected:false,created_ms,due_ms,spot_ingress:None});Ok(())
    }
    /// Internal Rust opt-in; existing live headers never call this method.
    pub fn enable_handoff(&mut self)->Result<()> {
        if self.control.handoff.is_some(){return Err("configuration handoff already enabled".into())}
        self.control.handoff=Some(Arc::new(Mutex::new(Slot::default())));Ok(())
    }
    fn target_state(&self)->Targets {Targets {
        perpetual:self.settings.perpetual_symbols.clone(),
        spot:self.settings.spot_symbols.clone(),
        native_spot:self.native_spot.iter().map(|(&venue, sub)| (venue, sub.instruments.clone())).collect(),
    }.normalize()}
    pub fn handoff_enabled(&self)->bool{self.control.handoff.is_some()}
    fn check_handoff(&self,targets:&Targets)->Result<()> {
        if self.pending_targets.is_some()||!self.retiring.is_empty()||self.workers.values().any(|w|w.replacement){return Err("configuration physical operation busy".into())}
        for symbols in [&targets.perpetual,&targets.spot] {
            if symbols.len()>MAX_SYMBOLS||symbols.iter().any(|s|!crate::symbol_valid(s))||symbols.iter().collect::<BTreeSet<_>>().len()!=symbols.len(){return Err("invalid configuration targets".into())}
        }
        if targets.perpetual.is_empty(){return Err("empty configuration perpetual targets".into())}
        if !targets.native_spot.keys().eq(self.native_spot.keys()) {
            return Err("configuration native spot owners cannot change".into());
        }
        for (&venue, instruments) in &targets.native_spot {
            let endpoint = &self.native_spot[&venue].endpoint;
            SpotSocket::validate(venue, endpoint.address, &endpoint.uri, instruments)?;
            if instruments.keys().any(|symbol| !targets.perpetual.contains(symbol)) {
                return Err("native spot target lacks perpetual binding".into());
            }
        }
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
            if worker.spot {if worker.generation>0&&!worker.token.is_empty(){rows.push(spot_route_row(&worker,false,&self.settings.spot_symbols,&self.native_spot));}}
            else if worker.generation>0 {rows.push(json!({"type":"feed","action":{"type":"retire","route":route}}));}
            self.generations.remove(&route);self.serials.remove(&route);self.retired+=1;
            self.retiring.insert(route,worker);
        }
        Ok(rows)
    }
    fn collect_worker_exit(&mut self,exit:WorkerExit,venue:Option<SpotVenue>)->Result<()> {
        let max_canceled = venue.map_or(1, |venue| venue.max_quote_rows().max(MAX_SYMBOLS));
        if exit.canceled_post_ack>max_canceled{return Err("native worker cancellation count invalid; owner incomplete".into())}
        if exit.canceled_post_ack==0{return Ok(())}
        let first=self.failure.lock().map_err(|_|"native failure lock poisoned")?;
        if first.is_none() {
            self.retired_frames=self.retired_frames.checked_add(exit.canceled_post_ack as u64).ok_or("native retired frame count overflow; owner incomplete")?;
        }
        Ok(())
    }
    fn reap_ready(&mut self)->Result<()> {
        let ready:Vec<_>=self.retiring.iter().filter(|(_,w)|w.join.as_ref().is_none_or(JoinHandle::is_finished)).map(|(r,_)|r.clone()).collect();
        for route in ready {
            let mut worker=self.retiring.remove(&route).unwrap();
            if let Some(join)=worker.join.take(){let exit=join.join().map_err(|_|"native retiring worker panicked")?;self.collect_worker_exit(exit,worker.venue)?;}
        }
        Ok(())
    }
    // Explicit final shutdown/destruction only. Keep every unfinished handle
    // owned and counted; reuse the destructor's cooperative wait outside truth.
    fn finish_retiring(&mut self)->Result<()> {
        let mut first=None;
        while !self.retiring.is_empty(){if let Err(error)=self.reap_ready(){if first.is_none(){first=Some(error)}}if !self.retiring.is_empty(){thread::sleep(Duration::from_millis(10));}}
        if let Some(error)=first{Err(error)}else{Ok(())}
    }
    fn progress_handoff_inner(&mut self)->Result<Vec<Value>> {
        let Some(slot)=self.control.handoff.clone() else{return Ok(vec![])};
        let command={let mut slot=slot.lock().map_err(|_|"configuration slot poisoned")?;if let Some(error)=slot.first_failure(){return Err(error)}if !matches!(slot.phase(),Some(handoff::Phase::Committed|handoff::Phase::Applying|handoff::Phase::Ready)){return Ok(vec![])}slot.command()};
        let mut rows=Vec::new();
        if let Some(plan)=command {
            if plan.before!=self.target_state(){return Err("configuration physical baseline changed".into())}
            self.check_handoff(&plan.after)?;
            let perp=plan.before.perpetual!=plan.after.perpetual;let spot=plan.before.spot!=plan.after.spot;
            let routes=self.workers.iter().filter(|(_,w)|match w.venue {
                Some(venue) => plan.before.native_spot.get(&venue) != plan.after.native_spot.get(&venue),
                None if w.spot => spot,
                None => perp,
            }).map(|(r,_)|r.clone()).collect();
            rows.extend(self.detach_handoff(routes)?);
            if perp {
                for s in plan.before.perpetual.iter().chain(&plan.after.perpetual){if plan.before.perpetual.contains(s)!=plan.after.perpetual.contains(s){*self.memberships.entry(s.clone()).or_default()+=1;}}
                rows.push(json!({"type":"feed","action":{"type":"targets","symbols":plan.after.perpetual}}));
            }
            if perp{self.settings.perpetual_symbols=plan.after.perpetual.clone();}if spot{self.settings.spot_symbols=plan.after.spot.clone();}
            for (&venue, instruments) in &plan.after.native_spot {self.native_spot.get_mut(&venue).unwrap().instruments=instruments.clone();}
            self.pending_targets=Some(plan);
        }
        self.reap_ready()?;
        if self.retiring.is_empty() {
            if let Some(plan)=self.pending_targets.take() {
                if plan.before.perpetual!=plan.after.perpetual {self.spawn(0,false,false)?;if self.settings.mode!="single"{self.spawn(1,false,false)?;}}
                if plan.before.spot!=plan.after.spot&&!plan.after.spot.is_empty(){self.spawn(0,true,false)?;}
                for (&venue, instruments) in &plan.after.native_spot {
                    if !instruments.is_empty() && plan.before.native_spot.get(&venue) != Some(instruments) {
                        self.spawn_worker(0,true,false,Some(venue))?;
                    }
                }
            }
            let truth=self.control.physical.lock().map_err(|_|"physical truth poisoned")?;
            let mut slot=slot.lock().map_err(|_|"configuration slot poisoned")?;
            let required_perpetual=if self.settings.mode=="single"{1}else{2};
            let expected_spot = usize::from(!self.settings.spot_symbols.is_empty()) + self.native_spot.values().filter(|sub| !sub.instruments.is_empty()).count();
            let targets = self.target_state();
            let qualified=self.workers.values().filter(|w|!w.spot).count()==required_perpetual&&self.workers.values().filter(|w|w.spot).count()==expected_spot&&self.workers.iter().all(|(route,w)| {
                truth.get(route).is_some_and(|s|!slot.requires_ack(s)||(w.connected&&s["connected"]==true&&s["token"]==w.token&&s["generation"]==w.generation&&s["applied"].as_array().and_then(|a|a.iter().map(Value::as_str).collect::<Option<BTreeSet<_>>>())==targets.applied(s)))
            });
            if qualified {slot.ready(&truth);}else{slot.not_ready();}
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
        if !self.retiring.is_empty(){return Err("native physical retirement busy".into())}
        if slot>1||self.workers.values().any(|w|w.replacement)||!self.workers.values().any(|w|!w.spot&&w.slot==slot&&!w.replacement){return Err("invalid or concurrent rotation".into())}
        self.spawn(slot,false,true)
    }
    /// Subscription changes retire all old subscription truth before reconnect.
    /// Perpetual make-before-break is reserved for rotations with identical targets.
    pub fn targets(&mut self,perpetual:Vec<String>,spot:Vec<String>)->Result<Vec<Value>> {
        if !self.native_spot.is_empty() {return Err("native spot targets require whole-plan handoff".into())}
        if self.handoff_fenced(){return Err("configuration physical operation busy".into())}
        if !self.retiring.is_empty(){return Err("native physical retirement busy".into())}
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
        if let Some(join)=worker.join.take(){let exit=join.join().map_err(|_|"native worker panicked")?;self.collect_worker_exit(exit,worker.venue)?;}
        self.control.physical.lock().map_err(|_|"physical truth poisoned")?.remove(route);
        self.retired+=1;self.generations.remove(route);self.serials.remove(route);
        Ok(if worker.spot {vec![spot_route_row(&worker,false,&self.settings.spot_symbols,&self.native_spot)]}else if worker.generation>0 {vec![json!({"type":"feed","action":{"type":"retire","route":route}})]}else{vec![]})
    }
    pub fn recv(&mut self,timeout:Duration)->Result<Vec<Value>> {
        let start=Instant::now();let result=self.recv_inner(timeout);self.recv_total.record(start);result
    }
    fn recv_inner(&mut self,timeout:Duration)->Result<Vec<Value>> {
        if let Some(error)=self.failure.lock().map_err(|_|"native failure lock poisoned")?.clone(){return Err(error)}
        self.reap_ready()?;
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
                        // worker truth updates; detached ownership never joins here.
                        let truth=self.control.physical.lock().map_err(|_|"physical truth poisoned")?;
                        if !truth.get(&route).is_some_and(|state|promotion_current(worker,state,&self.settings.perpetual_symbols)){return Ok(vec![])}
                        if let Some(old)=old.as_ref().and_then(|route|self.workers.get(route)){commit_promotion_stop(&truth,old.stop.as_ref());}
                    }
                    let mut rows=Vec::new();if let Some(old)=old{rows.extend(self.detach_handoff(vec![old])?);}
                    let worker=self.workers.get_mut(&route).unwrap();worker.replacement=false;worker.due_ms=self.lifecycle.rotation.promoted_due(now);
                    self.control.physical.lock().map_err(|_|"physical truth poisoned")?.get_mut(&route).ok_or("missing physical replacement")?["replacement"]=json!(false);
                    rows.push(json!({"type":"feed","action":{"type":"route","route":route,"slot":slot,"token":worker.token,"generation":worker.generation,"connected":worker.connected,"replacement":false,"applied":self.settings.perpetual_symbols}}));return Ok(rows)
                }
            }
        }
        if let Some((route,slot))=self.workers.iter().find(|(_,w)|w.replacement&&self.lifecycle.rotation.expired(w.created_ms,now)).map(|(id,w)|(id.clone(),w.slot)){
            let rows=self.detach_handoff(vec![route])?;if let Some(old)=self.workers.values_mut().find(|w|!w.spot&&!w.replacement&&w.slot==slot){old.due_ms=self.lifecycle.rotation.retry_due(now);}return Ok(rows)
        }
        if self.retiring.is_empty()&&self.settings.mode!="single"&&!self.settings.perpetual_symbols.is_empty()&&!self.workers.values().any(|w|w.replacement){
            if let Some(slot)=self.workers.values().filter(|w|!w.spot&&!w.replacement&&self.lifecycle.rotation.due(w.due_ms,now)).min_by_key(|w|w.due_ms).map(|w|w.slot){self.rotate(slot)?;}
        }
        }
        let waiting=Instant::now();let received=self.receive_event(timeout);self.channel_wait.record(waiting);
        let Some(event)=received? else{return Ok(vec![])};
        let route=match &event{Event::Connected{route,..}|Event::Frame{route,..}|Event::VenueQuote{route,..}|Event::Disconnected{route,..}=>route,_=>return Err("native ingress credit retained after dequeue; owner incomplete".into())};
        if !self.workers.contains_key(route){self.retired+=1;if event.is_quote(){self.retired_frames+=1;}return Ok(vec![])}
        let mut rows=Vec::new();
        match event {
            Event::Connected{route,token,generation}=>{
                let worker=self.workers.get_mut(&route).unwrap();
                if worker.venue.is_some() && (generation<=worker.generation || !venue_current(&self.control, &route, &token, generation)?) {
                    return Ok(vec![]);
                }
                worker.token=token.clone();worker.generation=generation;worker.connected=true;self.generations.insert(route.clone(),generation);
                if worker.spot && worker.venue.is_none() {
                    worker.spot_ingress = self.control.spot_ingress_generation(&token, generation);
                }
                if worker.spot{rows.push(spot_route_row(worker,true,&self.settings.spot_symbols,&self.native_spot));}
                else{rows.push(json!({"type":"feed","action":{"type":"route","route":route,"slot":worker.slot,"token":token,"generation":generation,"connected":true,"replacement":worker.replacement,"applied":self.settings.perpetual_symbols}}));}
            },
            Event::Disconnected{route,generation}=>{
                let worker=self.workers.get_mut(&route).unwrap();if worker.generation!=generation||generation==0{return Ok(vec![])}worker.connected=false;
                if worker.spot {rows.push(spot_route_row(worker,false,&self.settings.spot_symbols,&self.native_spot));}
                else {rows.push(json!({"type":"feed","action":{"type":"route","route":route,"slot":worker.slot,"token":worker.token,"generation":generation,"connected":false,"replacement":worker.replacement,"applied":[]}}));}
            },
            Event::VenueQuote{route,token,generation,quote,clock,batch_has_more}=>{
                let worker=&self.workers[&route];
                let venue=worker.venue.ok_or("missing native spot venue")?;
                if !worker.connected || worker.token!=token || worker.generation!=generation
                    || !venue_current(&self.control,&route,&token,generation)?
                    || !self.native_spot[&venue].instruments.contains_key(&quote.symbol) {
                    self.retired_frames+=1;
                    return Ok(vec![]);
                }
                let processing=self.clock();
                self.supplied+=1;
                self.queue_age.record(processing.monotonic_ms-clock.monotonic_ms);
                self.source_age.record(processing.wall_ms-quote.source_ms);
                rows.push(json!({"type":"native_spot_quote","venue":venue,"token":token,"generation":generation,"quote":quote,"now_ms":processing.wall_ms,"batch_has_more":batch_has_more}));
            },
            Event::Frame{route,token,generation,mut payload,clock}=>{
                let processing=self.clock();
                let configured=self.handoff_enabled();
                let worker=self.workers.get_mut(&route).unwrap();if worker.generation!=generation||worker.token!=token{return Err("physical frame token mismatch".into())}
                if configured {crate::reject_numeric_bookticker_fields(&payload)?;}
                let tick=crate::parse_bookticker(&payload,clock.wall_ms,!worker.spot)?;
                self.supplied+=1;
                self.queue_age.record(processing.monotonic_ms-clock.monotonic_ms);self.source_age.record(processing.wall_ms-tick.source_ms);
                if worker.spot {
                    if let Some(point) = worker.spot_ingress.as_ref().and_then(|probe| probe.slot(&payload)) {
                        point.dequeued.store(clock.monotonic_ms, Ordering::Release);
                    }
                    if let Some(mut stages) = worker.spot_ingress.as_ref()
                        .and_then(|probe| probe.receive.as_ref()).and_then(|state| state.lock().ok()) {
                        stages.dequeue(&tick.symbol, processing.monotonic_ms);
                    }
                    rows.push(json!({"type":"spot_raw","token":token,"generation":generation,"payload":payload,"now_ms":processing.wall_ms,"receive_ms":clock.wall_ms}));
                }
                else {
                    let membership=*self.memberships.get(&tick.symbol).ok_or("unsubscribed native frame")?;
                    payload["_ptk_connection_id"]=json!(token);payload["_ptk_connection_generation"]=json!(generation);payload["run_time_ms"]=json!(clock.wall_ms);
                    rows.push(json!({"type":"feed","action":{"type":"raw_captured","route":route,"payload":payload,"membership":membership,"serial":self.serials[&route],"wall_ms":processing.wall_ms,"monotonic_ms":processing.monotonic_ms,"ingress_clock":{"wall_ms":clock.wall_ms,"monotonic_ms":clock.monotonic_ms}}}));
                }
            },
            _=>return Err("native ingress credit retained after dequeue; owner incomplete".into()),
        }
        Ok(rows)
    }
    fn receive_event(&self,timeout:Duration)->Result<Option<Event>> {
        match self.receiver.try_recv() {
            Ok(event)=>return Ok(Some(event.release_credit())),
            Err(mpsc::TryRecvError::Disconnected)=>return Err("native ingress disconnected".into()),
            Err(mpsc::TryRecvError::Empty)=>{},
        }
        #[cfg(test)]
        if let Some(credits)=&self.ingress_credits{if let Some(reached)=credits.before_receive_wait.lock().unwrap().take(){reached.send(()).unwrap();}}
        if !timeout.is_zero(){self.control.wake.wait(timeout)?;}
        if let Some(error)=self.failure.lock().map_err(|_|"native failure lock poisoned")?.clone(){return Err(error)}
        // One queue check after the single bounded wait, including spurious or
        // control-only wakes; never spin and never consume more than one event.
        match self.receiver.try_recv() {
            Ok(event)=>Ok(Some(event.release_credit())),Err(mpsc::TryRecvError::Empty)=>Ok(None),
            Err(mpsc::TryRecvError::Disconnected)=>Err("native ingress disconnected".into()),
        }
    }
    pub fn accounting(&self)->Value {let mut report=json!({"wire_raw_received":self.received.load(Ordering::Acquire),"supplied":self.supplied,"retired_frames":self.retired_frames,"retired_events_or_workers":self.retired,"workers":self.workers.len()+self.retiring.len(),"acknowledged":self.workers.values().filter(|w|w.connected).count(),"capacity":CAPACITY,"queue_age":self.queue_age.value(),"source_age":self.source_age.value(),"first_failure":self.failure.lock().ok().and_then(|f|f.clone()),"recv_total":self.recv_total,"channel_wait":self.channel_wait});if let Some(status)=self.handoff_status(){report["configuration"]=status;}report}
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
        if let Err(error)=self.finish_retiring(){if first.is_none(){first=Some(error)}}
        let mut purged=0;while let Ok(event)=self.receiver.try_recv(){let event=event.release_credit();if event.is_quote(){purged+=1;}else{self.retired+=1;}}
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
    let _=self.finish_retiring();
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
    fn actual_spot_worker_probe_distinguishes_observed_offer_and_dequeue() {
        let fixture = Fixture::start(0, 0);
        fixture.quotes.store(false, Ordering::Release);
        let mut runtime = Runtime::start_with_modes(
            fixture.settings("single"), LifecyclePolicy::default(), true, IngressMode::V2, SpotProbe::Ingress,
        ).unwrap();
        let control = runtime.control();
        let deadline = Instant::now() + Duration::from_secs(3);
        while runtime.workers.values().any(|worker| !worker.connected) {
            assert!(Instant::now() < deadline);
            runtime.recv(Duration::from_millis(10)).unwrap();
        }
        let worker = runtime.workers.values().find(|worker| worker.spot).unwrap();
        let (token, generation) = (worker.token.clone(), worker.generation);
        let sample = || control.spot_ingress_clocks(&token, generation, "BTCUSDT");
        assert_eq!(sample(), Some(SpotIngressClocks { observed: None, offered: None, dequeued: None }));
        assert_eq!(control.spot_ingress_clocks(&token, generation + 1, "BTCUSDT"), None);
        assert_eq!(control.spot_ingress_clocks("retired", generation, "BTCUSDT"), None);
        assert_eq!(control.spot_ingress_clocks(&token, generation, "ETHUSDT"), None);

        fixture.quotes.store(true, Ordering::Release);
        let queued = loop {
            let point = sample().unwrap();
            if point.offered.is_some() { break point; }
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(1));
        };
        // No recv call since opening the quote producer.
        assert!(queued.observed >= queued.offered);
        assert_eq!(queued.dequeued, None);
        assert_eq!(runtime.supplied, 0);

        fixture.quiet(&mut runtime);
        let drained = sample().unwrap();
        assert!(drained.observed >= queued.observed);
        assert_eq!(drained.observed, drained.offered);
        assert_eq!(drained.offered, drained.dequeued);
        assert_eq!(runtime.received.load(Ordering::Acquire), runtime.supplied);
        runtime.request_handoff_shutdown().unwrap();
        let until = Instant::now() + Duration::from_secs(3);
        while !runtime.handoff_shutdown_ready().unwrap() {
            assert!(Instant::now() < until);
            thread::sleep(Duration::from_millis(1));
        }
        runtime.shutdown().unwrap();
        assert_eq!(sample(), None);
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
        Runtime{settings,workers:BTreeMap::new(),sender,receiver,native_spot:BTreeMap::new(),native_spot_generation:Arc::new(AtomicU64::new(0)),failure:control.failure.clone(),origin:control.origin,next:0,generations:BTreeMap::new(),memberships:BTreeMap::from([("ETHUSDT".into(),1)]),serials:BTreeMap::new(),supplied:0,retired:0,fixture_wall:control.fixture_wall.clone(),anchored:false,received:Arc::new(AtomicU64::new(0)),retired_frames:0,queue_age:AgeStats::default(),source_age:AgeStats::default(),control,recv_total:Timing::default(),channel_wait:Timing::default(),lifecycle:LifecyclePolicy::default(),retiring:BTreeMap::new(),pending_targets:None,ingress_mode:IngressMode::Off,ingress_credits:None}
    }

    #[test]
    fn native_route_detach_with_full_ingress_preserves_first_failure() {
        use std::net::TcpListener;
        use tungstenite::Message;

        let limit = Duration::from_secs(4);
        let poll = Duration::from_millis(5);
        for mode in [IngressMode::Off, IngressMode::V1] {
            for (acknowledged, fatal) in [(true, false), (false, false), (true, true)] {
                let listener = TcpListener::bind("127.0.0.1:0").unwrap();
                listener.set_nonblocking(true).unwrap();
                let address = listener.local_addr().unwrap();
                let mut runtime = empty_runtime();
                runtime.enable_handoff().unwrap();
                runtime.ingress_mode = mode;
                runtime.native_spot.insert(SpotVenue::Okx, SpotSubscription {
                    endpoint:Endpoint {address,uri:format!("ws://{address}/fixture")},
                    instruments:BTreeMap::from([("ETHUSDT".into(),"ETH-USDT".into())]),
                });
                runtime.spawn_worker(0,true,false,Some(SpotVenue::Okx)).unwrap();
                let deadline = Instant::now() + limit;
                let tcp = loop {
                    assert!(Instant::now()<deadline,"native route did not dial");
                    if let Ok((tcp,_)) = listener.accept() {break tcp;}
                    thread::sleep(poll);
                };
                tcp.set_read_timeout(Some(limit)).unwrap();
                tcp.set_write_timeout(Some(limit)).unwrap();
                let mut peer = tungstenite::accept(tcp).unwrap();
                assert!(matches!(peer.read().unwrap(),Message::Text(_)));
                let ack = Message::Text(json!({"event":"subscribe","arg":{"channel":"tickers","instId":"ETH-USDT"}}).to_string().into());
                if acknowledged {
                    peer.send(ack.clone()).unwrap();
                    loop {
                        assert!(Instant::now()<deadline,"native route did not ACK");
                        if runtime.recv(poll).unwrap().iter().any(|row| row["connected"]==true) {break;}
                    }
                }
                peer.send(Message::Ping(vec![1].into())).unwrap();
                assert_eq!(peer.read().unwrap(),Message::Pong(vec![1].into()));
                for _ in 0..CAPACITY {
                    assert!(runtime.sender.try_send(Event::Disconnected{route:"fixture-queue".into(),generation:1}).is_ok());
                }
                if fatal {
                    fail(&runtime.failure,"first fixture failure".into());
                }
                let retired = runtime.request_handoff_shutdown().unwrap();
                assert_eq!(retired.len(),usize::from(acknowledged));
                assert!(runtime.control.physical.lock().unwrap().is_empty());
                if !acknowledged {
                    let _ = peer.send(ack);
                }
                while !runtime.retiring.is_empty() {
                    assert!(Instant::now()<deadline,"native route did not retire");
                    runtime.reap_ready().unwrap();
                    thread::sleep(poll);
                }
                assert_eq!(runtime.receiver.try_iter().count(),CAPACITY);
                assert_eq!(runtime.failure.lock().unwrap().as_deref(),fatal.then_some("first fixture failure"));
                if fatal {
                    assert_eq!(runtime.handoff_shutdown_ready().unwrap_err(),"first fixture failure");
                } else {
                    assert!(runtime.handoff_shutdown_ready().unwrap());
                }
                runtime.shutdown().unwrap();
            }
        }
    }

    #[test]
    fn buffered_quote_order_uses_sequence_then_source_and_decimal_equality() {
        let quote = |sequence, source_ms, bid: &str| SpotQuote {
            symbol:"ETHUSDT".into(),bid:bid.into(),ask:"2".into(),source_ms,receive_ms:99,sequence,
        };
        let old = quote(Some(3),20,"1.00");
        assert!(buffered_quote_is_old(&old,&quote(Some(2),21,"1")));
        assert!(buffered_quote_is_old(&old,&quote(Some(3),21,"1")));
        assert!(!buffered_quote_is_old(&old,&quote(Some(4),19,"1")));
        assert!(buffered_quote_is_old(&old,&quote(None,19,"1.1")));
        assert!(buffered_quote_is_old(&old,&quote(None,20,"1e0")));
        assert!(!buffered_quote_is_old(&old,&quote(None,20,"1.1")));
        assert!(!buffered_quote_is_old(&old,&quote(None,21,"1")));
    }

    #[test]
    fn configuration_retirement_progress_keeps_status_responsive_with_real_unfinished_handle(){
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();
        let release=Arc::new(AtomicBool::new(false));let _release=Release(release.clone());
        let stopped=Arc::new(AtomicBool::new(false));let physical=runtime.control.physical.clone();
        physical.lock().unwrap().insert("old".into(),json!({"token":"old-token","generation":1,"connected":true,"applied":["ETHUSDT"]}));
        let resurrected=Arc::new(AtomicBool::new(false));let checked=resurrected.clone();
        let join=thread::spawn(move||{while !release.load(Ordering::Acquire){thread::sleep(Duration::from_millis(1));}if let Some(row)=physical.lock().unwrap().get_mut("old"){row["connected"]=json!(true);checked.store(true,Ordering::Release);}WorkerExit::default()});
        runtime.workers.insert("old".into(),Worker{stop:stopped.clone(),join:Some(join),slot:0,spot:false,venue:None,replacement:false,token:"old-token".into(),generation:1,connected:true,created_ms:0,due_ms:u64::MAX,spot_ingress:None});
        let targets=Targets{perpetual:vec!["BTCUSDT".into()],spot:vec![],native_spot:Default::default()};runtime.prepare_handoff(1,targets.clone()).unwrap();
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
        runtime.workers.insert("old".into(),Worker{stop:Arc::new(AtomicBool::new(false)),join:None,slot:0,spot:false,venue:None,replacement:false,token:"token".into(),generation:7,connected:true,created_ms:4,due_ms:99,spot_ingress:None});
        runtime.control.physical.lock().unwrap().insert("old".into(),json!({"token":"token","generation":7,"connected":true,"applied":["ETHUSDT","BTCUSDT"]}));
        runtime.workers.insert("spot".into(),Worker{stop:Arc::new(AtomicBool::new(false)),join:None,slot:0,spot:true,venue:None,replacement:false,token:"spot-token".into(),generation:8,connected:true,created_ms:5,due_ms:100,spot_ingress:None});
        runtime.control.physical.lock().unwrap().insert("spot".into(),json!({"token":"spot-token","generation":8,"connected":true,"applied":["ETHUSDT","BTCUSDT"]}));
        let memberships=runtime.memberships.clone();let truth=runtime.control.physical.lock().unwrap().clone();
        runtime.prepare_handoff(1,Targets{perpetual:vec!["BTCUSDT".into(),"ETHUSDT".into()],spot:vec!["BTCUSDT".into(),"ETHUSDT".into()],native_spot:Default::default()}).unwrap();
        runtime.control.handoff.as_ref().unwrap().lock().unwrap().commit_logical(1,||Ok(())).unwrap();
        assert!(runtime.progress_handoff().unwrap().is_empty());assert_eq!(runtime.memberships,memberships);assert_eq!(*runtime.control.physical.lock().unwrap(),truth);
        assert_eq!((runtime.workers["old"].token.as_str(),runtime.workers["old"].generation,runtime.workers["old"].due_ms),("token",7,99));assert!(runtime.retiring.is_empty());
        assert_eq!(runtime.control.handoff.as_ref().unwrap().lock().unwrap().status().0.unwrap().phase,Phase::Ready);assert!(runtime.handoff_fenced());
        runtime.control.handoff.as_ref().unwrap().lock().unwrap().commit_logical(1,||panic!("duplicate logical mutation")).unwrap();assert!(runtime.progress_handoff().unwrap().is_empty());
    }
    #[test]
    fn configuration_preexisting_failure_wins_without_side_effects(){
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();runtime.prepare_handoff(1,Targets{perpetual:vec!["BTCUSDT".into()],spot:vec![],native_spot:Default::default()}).unwrap();
        fail(&runtime.failure,"original transport failure".into());
        assert_eq!(runtime.progress_handoff().unwrap_err(),"original transport failure");
        let slot=runtime.control.handoff.as_ref().unwrap().lock().unwrap();assert_eq!(slot.status().0.unwrap().phase,Phase::Failed);assert_eq!(slot.first_failure().as_deref(),Some("original transport failure"));
        assert_eq!(runtime.settings.perpetual_symbols,["ETHUSDT"]);assert!(runtime.pending_targets.is_none());
    }
    #[test]
    fn configuration_slot_is_bounded_pure_and_preserves_first_failure(){
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();let before=runtime.target_state();
        let targets=Targets{perpetual:vec!["BTCUSDT".into()],spot:vec![],native_spot:Default::default()};runtime.prepare_handoff(1,targets.clone()).unwrap();
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
mod rotation_reclamation_tests {
    use super::*;
    use super::handoff_tests::empty_runtime;

    // Unit witness only: a real unfinished OS thread deliberately remains in a
    // bounded internal stall after stop. This is not socket/child evidence.
    struct Release(Arc<(Mutex<bool>,Condvar)>);
    impl Release {
        fn release(&self) {
            *self.0.0.lock().unwrap_or_else(|e|e.into_inner())=true;
            self.0.1.notify_all();
        }
    }
    impl Drop for Release {fn drop(&mut self){self.release();}}

    fn worker(token:&str,generation:u64,replacement:bool)->Worker {
        Worker{stop:Arc::new(AtomicBool::new(false)),join:None,slot:0,spot:false,venue:None,replacement,token:token.into(),generation,connected:true,created_ms:0,due_ms:u64::MAX,spot_ingress:None}
    }
    fn install(runtime:&mut Runtime,route:&str,worker:Worker) {
        runtime.control.physical.lock().unwrap().insert(route.into(),json!({"type":"route","route":route,"slot":worker.slot,"spot":worker.spot,"token":worker.token,"generation":worker.generation,"connected":true,"replacement":worker.replacement,"applied":["ETHUSDT"]}));
        runtime.generations.insert(route.into(),worker.generation);
        runtime.serials.insert(route.into(),if worker.replacement{9}else{4});
        runtime.workers.insert(route.into(),worker);
    }
    fn rotation_recv_retains_live_worker(promote:bool) {
        let mut runtime=empty_runtime();
        // Declared after Runtime: assertion unwinding releases the stall before
        // the ownership-preserving Runtime destructor can wait for its thread.
        let release=Release(Arc::new((Mutex::new(false),Condvar::new())));
        let retired_route=if promote{"old"}else{"candidate"};
        let mut old=worker("old-token",7,false);
        let mut candidate=worker("candidate-token",8,true);
        let stopped=if promote{old.stop.clone()}else{candidate.stop.clone()};
        let (started,ready)=mpsc::sync_channel(1);
        let physical=runtime.control.physical.clone();let held=release.0.clone();let stop=stopped.clone();
        let recreated=Arc::new(AtomicBool::new(false));let callback=recreated.clone();
        let join=thread::spawn(move|| {
            started.send(()).unwrap();let until=Instant::now()+Duration::from_secs(3);
            while !stop.load(Ordering::Acquire)&&!*held.0.lock().unwrap()&&Instant::now()<until{thread::sleep(Duration::from_millis(1));}
            if stop.load(Ordering::Acquire) {
                // The old join path must exceed the unchanged 250ms RPC budget;
                // the timeout also bounds the expected-red run without cleanup.
                let guard=held.0.lock().unwrap();
                drop(held.1.wait_timeout_while(guard,Duration::from_millis(600),|released|!*released).unwrap());
                // Same get_mut-only shape as the real late ACK callback.
                if let Some(state)=physical.lock().unwrap().get_mut(retired_route){state["connected"]=json!(true);callback.store(true,Ordering::Release);}
            }
            WorkerExit::default()
        });
        if promote{old.join=Some(join);}else{candidate.join=Some(join);}
        install(&mut runtime,"old",old);install(&mut runtime,"candidate",candidate);
        let mut spot=worker("spot-token",3,false);spot.spot=true;install(&mut runtime,"spot",spot);
        ready.recv_timeout(Duration::from_secs(1)).unwrap();
        assert!(!runtime.workers[retired_route].join.as_ref().unwrap().is_finished());
        if promote {*runtime.control.promotion.lock().unwrap()=Some("candidate".into());}
        else {runtime.lifecycle.rotation.timeout_ms=0;}
        let memberships=runtime.memberships.clone();let survivor=if promote{"candidate"}else{"old"};
        let survivor_token=runtime.workers[survivor].token.clone();let survivor_generation=runtime.workers[survivor].generation;let survivor_serial=runtime.serials[survivor];
        runtime.sender.send(Event::Connected{route:retired_route.into(),token:"test-late1".into(),generation:99}).unwrap();
        runtime.sender.send(Event::Frame{route:retired_route.into(),token:"test-late1".into(),generation:99,payload:json!({}),clock:runtime.clock()}).unwrap();

        let before=runtime.origin.elapsed().as_millis() as u64;let start=Instant::now();
        let rows=runtime.recv(Duration::ZERO).unwrap();let elapsed=start.elapsed();let after=runtime.origin.elapsed().as_millis() as u64;
        assert!(elapsed<Duration::from_millis(250),"rotation recv joined an unfinished worker: promote={promote}, elapsed={elapsed:?}");
        assert!(stopped.load(Ordering::Acquire));assert!(!runtime.workers.contains_key(retired_route));
        assert_eq!((runtime.workers.len(),runtime.retiring.len()),(2,1));
        assert!(!runtime.retiring[retired_route].join.as_ref().unwrap().is_finished());
        assert!(!runtime.control.physical.lock().unwrap().contains_key(retired_route));
        assert_eq!(rows.iter().filter(|row|row["action"]["type"]=="retire").cloned().collect::<Vec<_>>(),vec![json!({"type":"feed","action":{"type":"retire","route":retired_route}})]);
        assert_eq!(rows.len(),if promote{2}else{1});
        let due=runtime.workers[survivor].due_ms;let interval=if promote{runtime.lifecycle.rotation.rotation_ms}else{runtime.lifecycle.rotation.retry_ms};
        assert!(due>=before+interval&&due<=after+interval);
        assert!(!runtime.workers[survivor].replacement&&runtime.workers[survivor].connected);
        assert_eq!((runtime.workers[survivor].token.as_str(),runtime.workers[survivor].generation,runtime.serials[survivor]),(survivor_token.as_str(),survivor_generation,survivor_serial));
        assert_eq!(runtime.memberships,memberships);
        assert_eq!(runtime.accounting()["workers"],3,"live retired handle must remain counted");
        assert_eq!(runtime.failure_progress().retiring_workers,1);
        let next=runtime.next;assert!(runtime.rotate(0).is_err(),"unfinished retirement must fence another replacement");assert_eq!(runtime.next,next);
        assert!(runtime.targets(vec!["BTCUSDT".into()],vec!["BTCUSDT".into()]).is_err(),"unfinished rotation must fence new physical work");
        assert_eq!(runtime.settings.perpetual_symbols,["ETHUSDT"]);assert_eq!(runtime.memberships,memberships);
        for _ in 0..2 {let start=Instant::now();assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());assert!(start.elapsed()<Duration::from_millis(250));}
        assert_eq!((runtime.supplied,runtime.retired_frames,runtime.retired),(0,1,3));
        assert!(!runtime.generations.contains_key(retired_route)&&!runtime.serials.contains_key(retired_route));
        assert_eq!((runtime.workers.len(),runtime.retiring.len()),(2,1));
        release.release();let until=Instant::now()+Duration::from_secs(2);
        while !runtime.retiring[retired_route].join.as_ref().unwrap().is_finished(){assert!(Instant::now()<until);thread::sleep(Duration::from_millis(1));}
        // Ordinary nonconfigured recv owns reaping; no handoff/control required.
        assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());assert!(runtime.retiring.is_empty());
        assert_eq!(runtime.accounting()["workers"],2);assert!(!recreated.load(Ordering::Acquire));
        assert!(runtime.failure.lock().unwrap().is_none());runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
    }
    #[test]
    fn rotation_promotion_recv_keeps_unfinished_retirement_off_actor_lane(){rotation_recv_retains_live_worker(true);}
    #[test]
    fn rotation_timeout_recv_keeps_healthy_old_route_and_unfinished_retirement_off_actor_lane(){rotation_recv_retains_live_worker(false);}
    #[test]
    fn automatic_rotation_waits_for_unfinished_other_slot_retirement_then_resumes(){
        for mode in ["shadow","redundant"] {
            let mut runtime=empty_runtime();runtime.settings.mode=mode.into();
            // Unit ownership witness, not socket/child qualification. Declare
            // release after Runtime so unwinding cannot strand its old handle.
            let release=Release(Arc::new((Mutex::new(false),Condvar::new())));
            let (started,ready)=mpsc::sync_channel(1);let held=release.0.clone();
            let mut old=worker("test-auto-old",7,false);let old_stop=old.stop.clone();
            old.join=Some(thread::spawn(move||{
                started.send(()).unwrap();let guard=held.0.lock().unwrap();
                drop(held.1.wait_timeout_while(guard,Duration::from_secs(5),|released|!*released).unwrap());
                WorkerExit::default()
            }));
            install(&mut runtime,"old-slot0",old);
            install(&mut runtime,"current-slot0",worker("test-auto-current",8,false));
            let mut other=worker("test-auto-other",9,false);other.slot=1;other.due_ms=0;
            let stop=other.stop.clone();let (started_other,ready_other)=mpsc::sync_channel(1);
            other.join=Some(thread::spawn(move||{
                started_other.send(()).unwrap();let until=Instant::now()+Duration::from_secs(5);
                while !stop.load(Ordering::Acquire)&&Instant::now()<until{thread::sleep(Duration::from_millis(1));}
                WorkerExit::default()
            }));
            install(&mut runtime,"other-slot1",other);
            let mut spot=worker("test-auto-spot",3,false);spot.spot=true;install(&mut runtime,"spot",spot);
            ready.recv_timeout(Duration::from_secs(1)).unwrap();ready_other.recv_timeout(Duration::from_secs(1)).unwrap();
            assert_eq!(runtime.detach_handoff(vec!["old-slot0".into()]).unwrap(),vec![json!({"type":"feed","action":{"type":"retire","route":"old-slot0"}})]);
            assert!(old_stop.load(Ordering::Acquire));let next=runtime.next;
            for _ in 0..8 {
                assert!(!runtime.retiring["old-slot0"].join.as_ref().unwrap().is_finished());
                assert!(!runtime.workers["other-slot1"].join.as_ref().unwrap().is_finished());
                assert!(runtime.workers["other-slot1"].connected);
                assert!(runtime.lifecycle.rotation.due(runtime.workers["other-slot1"].due_ms,runtime.origin.elapsed().as_millis() as u64));
                let start=Instant::now();let rows=runtime.recv(Duration::ZERO).expect("due other slot must defer automatic rotation while retirement is unfinished");
                assert!(start.elapsed()<Duration::from_millis(250));assert!(rows.is_empty());
                assert_eq!(runtime.next,next);assert!(!runtime.workers.values().any(|w|w.replacement));
                assert_eq!((runtime.workers.len(),runtime.retiring.len()),(3,1));assert_eq!(runtime.accounting()["workers"],4);
                assert_eq!(runtime.workers.values().chain(runtime.retiring.values()).filter(|w|!w.spot).count(),3);
                assert_eq!(runtime.workers.values().chain(runtime.retiring.values()).filter(|w|w.spot).count(),1);
                assert!(!runtime.control.physical.lock().unwrap().contains_key("old-slot0"));
                assert!(runtime.failure.lock().unwrap().is_none());
            }
            release.release();let until=Instant::now()+Duration::from_secs(2);
            while !runtime.retiring["old-slot0"].join.as_ref().unwrap().is_finished(){assert!(Instant::now()<until);thread::sleep(Duration::from_millis(1));}
            // The same ordinary boundary reaps the actual finished handle and
            // takes the original automatic path for the still-due other slot.
            runtime.recv(Duration::ZERO).unwrap();assert!(runtime.retiring.is_empty());assert_eq!(runtime.next,next+1);
            let replacements:Vec<_>=runtime.workers.values().filter(|w|w.replacement).collect();assert_eq!(replacements.len(),1);
            let replacement=replacements[0];assert_eq!(replacement.slot,1);assert!(replacement.join.is_some());
            assert_eq!(replacement.due_ms,runtime.lifecycle.rotation.initial_due(replacement.created_ms,1));
            assert!(runtime.workers.contains_key("current-slot0")&&runtime.workers.contains_key("other-slot1"));
            assert_eq!(runtime.workers.len(),4);assert_eq!(runtime.accounting()["workers"],4);
            assert!(runtime.failure.lock().unwrap().is_none());runtime.shutdown().unwrap();assert_eq!(runtime.accounting()["workers"],0);
        }
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

#[cfg(test)]
mod worker_payload_tests {
    use super::*;
    use std::hint::black_box;

    const ITERATIONS:usize=8192;
    const CAPTURED:Clock=Clock{wall_ms:1_800_000_000_123,monotonic_ms:42};
    type Extraction=fn(&mut Value)->Value;

    fn reference_payload(event:&mut Value)->Value {event["payload"].clone()}
    fn envelope(sequence:u64)->Value {
        json!({"type":"raw","transport_note":{"retained":[true,null,"unchanged"]},"payload":{
            "e":"bookTicker","s":"BTCUSDT","u":sequence,"E":1_800_000_000_100_i64,
            "T":1_800_000_000_099_i64,"b":"999999999.99999999","a":"1000000000.00000000",
            "B":"0.00000001","A":"1.23456789","extra":{"array":[null,true,"verbatim"]}
        }})
    }
    fn canonical(payload:&Value,perpetual:bool)->Result<Value> {
        crate::reject_numeric_bookticker_fields(payload)?;
        crate::parse_bookticker(payload,CAPTURED.wall_ms,perpetual)
            .map(|tick|serde_json::to_value(tick).unwrap())
    }
    fn frame(payload:Value)->Event {
        Event::Frame{route:"unit-physical-route".into(),token:"test-worker-connection".into(),generation:7,payload,clock:CAPTURED}
    }

    #[test]
    fn values_match_old_index_clone_without_changing_siblings_or_envelope_type() {
        let mut cases=vec![envelope(17),json!({"type":"raw","other":[1,"retained"]})];
        for payload in [Value::Null,json!("owned text"),json!(19),json!(true),json!([null,"text",{"b":"1"}])] {
            cases.push(json!({"type":"raw","payload":payload,"other":{"untouched":[1,null]}}));
        }
        cases.extend([Value::Null,json!(false),json!(23),json!("nonobject"),json!(["payload",{"payload":"nested"}])]);
        for original in cases {
            let mut reference=original.clone();let mut production=original.clone();
            let expected=reference_payload(&mut reference);let actual=extract_worker_payload(&mut production);
            assert_eq!(actual,expected);assert_eq!(reference,original);
            // Compare every non-payload field and the original JSON variant.
            if let (Some(before),Some(after))=(original.as_object(),production.as_object()) {
                assert_eq!(before.len(),after.len());
                for (key,value) in before {if key!="payload" {assert_eq!(after.get(key),Some(value));}}
                assert_eq!(before.contains_key("payload"),after.contains_key("payload"));
            } else {assert_eq!(production,original);}
        }
    }

    #[test]
    fn allocation_identity_transfers_existing_string_and_consumes_only_payload() {
        let mut owned=envelope(18);
        let address=owned["payload"]["b"].as_str().unwrap().as_ptr();
        let reference=reference_payload(&mut owned);
        assert_ne!(reference["b"].as_str().unwrap().as_ptr(),address,"old clone must allocate distinct live storage");
        let actual=extract_worker_payload(&mut owned);
        assert_eq!(actual,reference);
        // Both the reference clone and the envelope remain live at comparison;
        // baseline storage cannot be freed and reused by the allocator.
        black_box(&reference);black_box(&owned);
        assert_eq!(actual["b"].as_str().unwrap().as_ptr(),address,"worker must transfer the original nonempty String allocation");
        let mut expected=envelope(18);expected["payload"]=Value::Null;
        assert_eq!(owned,expected,"only the existing payload slot may become null");
    }

    #[test]
    fn explicit_clock_and_canonical_parse_results_and_errors_match() {
        let base=envelope(19)["payload"].clone();
        let mut missing_source=base.clone();missing_source.as_object_mut().unwrap().remove("E");missing_source.as_object_mut().unwrap().remove("T");
        let mut null_e=base.clone();null_e["E"]=Value::Null;
        let mut malformed=base.clone();malformed["B"]=json!("0.0000000000000000001bad");
        let mut crossed=base.clone();crossed["b"]=json!("1000000000.00000001");
        let mut numeric=base.clone();numeric["a"]=json!(1000000000);
        let mut invalid_time=base.clone();invalid_time["E"]=json!("1800000000100");
        let cases=[(base,true,true),(null_e,true,true),(missing_source.clone(),false,true),
            (missing_source,true,false),(malformed,true,false),(crossed,true,false),
            (numeric,true,false),(invalid_time,true,false),(Value::Null,true,false)];
        for (payload,perpetual,valid) in cases {
            let mut original=json!({"type":"raw","payload":payload});let mut owned=original.clone();
            let reference=reference_payload(&mut original);let actual=extract_worker_payload(&mut owned);
            let expected=canonical(&reference,perpetual);let received=canonical(&actual,perpetual);
            assert_eq!(received,expected);assert_eq!(received.is_ok(),valid);
            if let Ok(tick)=received {
                assert_eq!(tick["receive_ms"],CAPTURED.wall_ms);
                assert_eq!(tick["bid"],"999999999.99999999");assert_eq!(tick["ask"],"1000000000.00000000");
                assert_eq!(tick["bid_size"],"0.00000001");assert_eq!(tick["ask_size"],"1.23456789");
                let source=if perpetual {reference["E"].as_i64().or_else(||reference["T"].as_i64()).unwrap()}else{CAPTURED.wall_ms};
                assert_eq!(tick["source_ms"],source);
            }
        }
        assert_eq!((CAPTURED.wall_ms,CAPTURED.monotonic_ms),(1_800_000_000_123,42));
    }

    #[test]
    fn actual_bounded_frame_handoff_keeps_payload_token_generation_and_clock() {
        assert_eq!(CAPACITY,4096);
        let (sender,receiver)=mpsc::sync_channel::<Event>(CAPACITY);
        let failure=Mutex::new(None);let wake=WakeSignal::default();
        let mut owned=envelope(20);let expected=reference_payload(&mut owned);
        let payload=extract_worker_payload(&mut owned);
        // Controlled pre-ACK ownership, using the same owned (payload,Clock)
        // tuple and actual Event::Frame/emit seam; no socket or selector claim.
        let mut before_ack=vec![(payload,CAPTURED)];
        for (payload,clock) in before_ack.drain(..) {
            assert!(emit_notified(&sender,&failure,&wake,Event::Frame{route:"unit-physical-route".into(),token:"test-worker-connection".into(),generation:7,payload,clock}));
        }
        let Event::Frame{route,token,generation,payload,clock}=receiver.try_recv().ok().unwrap() else{panic!("expected actual Frame")};
        assert_eq!(route,"unit-physical-route");assert_eq!(token,"test-worker-connection");assert_eq!(generation,7);
        assert_eq!((clock.wall_ms,clock.monotonic_ms),(CAPTURED.wall_ms,CAPTURED.monotonic_ms));
        assert_eq!(payload,expected);assert_eq!(canonical(&payload,true),canonical(&expected,true));
        assert!(before_ack.is_empty());assert!(failure.lock().unwrap().is_none());
        assert!(*wake.state.0.lock().unwrap());assert!(matches!(receiver.try_recv(),Err(mpsc::TryRecvError::Empty)));
    }

    fn check_actual_full(prior:Option<&str>) {
        let (sender,receiver)=mpsc::sync_channel::<Event>(CAPACITY);
        let failure=Mutex::new(prior.map(str::to_owned));let wake=WakeSignal::default();
        for _ in 0..CAPACITY {assert!(emit(&sender,&failure,frame(Value::Null)));}
        let mut owned=envelope(21);let payload=extract_worker_payload(&mut owned);
        assert!(!emit_notified(&sender,&failure,&wake,frame(payload)));
        let expected=prior.unwrap_or("native ingress full; owner incomplete");
        assert_eq!(failure.lock().unwrap().as_deref(),Some(expected));
        fail_notified(&failure,&wake,"later failure".into());
        assert_eq!(failure.lock().unwrap().as_deref(),Some(expected));assert!(*wake.state.0.lock().unwrap());
        let mut received=0;
        while let Ok(event)=receiver.try_recv() {let Event::Frame{payload,..}=event else{panic!("expected queued Frame")};assert!(payload.is_null());received+=1;}
        assert_eq!(received,CAPACITY);
    }
    struct ReturnedFrame<'a> {
        frame:Option<Event>,failure:&'a Mutex<Option<String>>,wake:&'a WakeSignal,
        observed:&'a Mutex<Option<(bool,bool)>>,
    }
    impl Drop for ReturnedFrame<'_> {
        fn drop(&mut self) {
            // Release the real payload first, then observe the unmodified
            // production generic emit's failure-latch / wake boundary.
            drop(self.frame.take());
            *self.observed.lock().unwrap()=Some((self.failure.lock().unwrap().is_none(),!*self.wake.state.0.lock().unwrap()));
        }
    }
    #[test]
    fn full_releases_returned_frame_before_latching_or_waking_and_keeps_first_error() {
        check_actual_full(None);check_actual_full(Some("original first error"));
        let failure=Mutex::new(None);let wake=WakeSignal::default();let observed=Mutex::new(None);
        let (sender,receiver)=mpsc::sync_channel::<Option<ReturnedFrame<'_>>>(CAPACITY);
        for _ in 0..CAPACITY {sender.try_send(None).ok().unwrap();}
        let mut owned=envelope(22);let payload=extract_worker_payload(&mut owned);
        let returned=ReturnedFrame{frame:Some(frame(payload)),failure:&failure,wake:&wake,observed:&observed};
        assert!(!emit_notified(&sender,&failure,&wake,Some(returned)));
        assert_eq!(*observed.lock().unwrap(),Some((true,true)));
        assert_eq!(failure.lock().unwrap().as_deref(),Some("native ingress full; owner incomplete"));
        assert!(*wake.state.0.lock().unwrap());drop(receiver);
    }

    fn prepared_envelopes()->Vec<Value> {(1..=ITERATIONS as u64).map(envelope).collect()}
    fn observe_component(envelopes:&mut [Value],extract:Extraction)->(u128,u64) {
        let mut outputs=Vec::with_capacity(ITERATIONS);
        let started=Instant::now();
        for envelope in envelopes {outputs.push(black_box(extract(black_box(envelope))));}
        let total_wall_ns=started.elapsed().as_nanos();
        // Full-value checksum and destruction are outside the extraction range.
        let checksum=outputs.iter().flat_map(|payload|serde_json::to_vec(payload).unwrap())
            .fold(0_u64,|sum,byte|sum.wrapping_mul(31).wrapping_add(u64::from(byte)));
        black_box(&outputs);(total_wall_ns,black_box(checksum))
    }
    #[test]
    fn fixed_same_input_component_cost_in_two_predeclared_orders() {
        let orders:[(&str,[(&str,Extraction);2]);2]=[
            ("reference_then_production",[("reference",reference_payload),("production",extract_worker_payload)]),
            ("production_then_reference",[("production",extract_worker_payload),("reference",reference_payload)]),
        ];
        let mut common_checksum=None;
        for (order,branches) in orders {
            // Both branches own the same 8192 inputs before either timer starts.
            let mut prepared=[prepared_envelopes(),prepared_envelopes()];
            for ((branch,extract),envelopes) in branches.into_iter().zip(prepared.iter_mut()) {
                assert_eq!(envelopes.len(),ITERATIONS);
                let (total_wall_ns,checksum)=observe_component(envelopes,extract);
                if let Some(expected)=common_checksum {assert_eq!(checksum,expected);} else{common_checksum=Some(checksum);}
                eprintln!("worker_payload_component order={order} branch={branch} iterations={ITERATIONS} total_wall_ns={total_wall_ns} checksum={checksum}");
            }
        }
    }
}

#[cfg(test)]
mod cooperative_ingress_tests {
    use super::*;

    const FRAMES:usize=8;
    fn payload(sequence:u64)->Value {
        json!({"e":"bookTicker","s":"BTCUSDT","u":sequence,"E":1_800_000_000_100_i64,
            "b":"999999999.99999999","a":"1000000000.00000000",
            "B":"0.00000001","A":"1.23456789","extra":[null,true,"verbatim"]})
    }
    fn frame(sequence:u64)->(Event,usize) {
        let mut raw=json!({"type":"raw","payload":payload(sequence)});
        let address=raw["payload"]["b"].as_str().unwrap().as_ptr() as usize;
        let payload=extract_worker_payload(&mut raw);
        assert!(raw["payload"].is_null());
        (Event::Frame{route:"unit-cooperative-route".into(),token:"test-cooperative-connection".into(),
            generation:7,payload,clock:Clock{wall_ms:1_800_000_000_123+sequence as i64,monotonic_ms:42+sequence as i64}},address)
    }
    fn observe_frame(event:Event,sequence:u64)->usize {
        let Event::Frame{route,token,generation,payload:received,clock}=event else{panic!("expected actual raw Frame")};
        assert_eq!(route,"unit-cooperative-route");assert_eq!(token,"test-cooperative-connection");assert_eq!(generation,7);
        assert_eq!(received,payload(sequence));
        assert_eq!((clock.wall_ms,clock.monotonic_ms),(1_800_000_000_123+sequence as i64,42+sequence as i64));
        received["b"].as_str().unwrap().as_ptr() as usize
    }

    #[test]
    fn exact_v1_gate_defaults_off_without_environment_mutation() {
        assert!(!ingress_cooperate(None));
        for value in ["","v0","v2","V1","1","true"," v1","v1 ","v1\n","v1\0"] {
            assert!(!ingress_cooperate(Some(std::ffi::OsStr::new(value))),"unexpected opt-in: {value:?}");
        }
        assert!(ingress_cooperate(Some(std::ffi::OsStr::new("v1"))));
        #[cfg(unix)] {
            use std::os::unix::ffi::OsStrExt;
            assert!(!ingress_cooperate(Some(std::ffi::OsStr::from_bytes(b"v1\xff"))));
            assert!(!ingress_cooperate(Some(std::ffi::OsStr::from_bytes(b"\xff"))));
        }
    }

    #[test]
    fn successful_frames_preserve_fifo_identity_clock_and_schedule_after_wake() {
        assert_eq!(CAPACITY,4096);
        let (sender,receiver)=mpsc::sync_channel::<Event>(CAPACITY);
        let failure=Mutex::new(None);let wake=WakeSignal::default();let mut scheduled=0;
        let mut expected=Vec::new();
        for sequence in 0..FRAMES as u64 {
            let (event,address)=frame(sequence);expected.push(address);
            assert!(emit_cooperatively(&sender,&failure,&wake,event,||{
                assert!(failure.try_lock().expect("failure lock retained during scheduling").is_none());
                let mut pending=wake.state.0.try_lock().expect("wake lock retained during scheduling");
                assert!(*pending,"original wake must precede scheduling");*pending=false;drop(pending);
                scheduled+=1;
                // Keep earlier frames queued until the final callback, so this
                // observes both actual enqueue-before-callback and FIFO order.
                if sequence+1==FRAMES as u64 {
                    for (sequence,address) in expected.iter().enumerate() {
                        let event=receiver.try_recv().ok().expect("successful frame absent at scheduling callback");
                        assert_eq!(observe_frame(event,sequence as u64),*address);
                    }
                    assert!(matches!(receiver.try_recv(),Err(mpsc::TryRecvError::Empty)));
                }
            }));
        }
        assert_eq!(scheduled,FRAMES);assert!(failure.lock().unwrap().is_none());
        drop(sender);assert!(matches!(receiver.try_recv(),Err(mpsc::TryRecvError::Disconnected)));
    }

    fn rejected_event(disconnected:bool,prior:Option<&str>) {
        let failure=Mutex::new(prior.map(str::to_owned));let wake=WakeSignal::default();
        let (sender,receiver)=mpsc::sync_channel::<Event>(CAPACITY);
        let receiver=if disconnected {drop(receiver);None}else{Some(receiver)};
        let mut queued=Vec::new();
        if !disconnected {
            for sequence in 0..CAPACITY as u64 {
                let (event,address)=frame(sequence);queued.push(address);assert!(emit(&sender,&failure,event));
            }
        }
        let mut scheduled=0;let (event,_)=frame(CAPACITY as u64);
        assert!(!emit_cooperatively(&sender,&failure,&wake,event,||scheduled+=1));
        assert_eq!(scheduled,0);
        let reason=if disconnected {"native ingress receiver disconnected; owner incomplete"}else{"native ingress full; owner incomplete"};
        let expected=prior.unwrap_or(reason);
        assert_eq!(failure.try_lock().unwrap().as_deref(),Some(expected));
        assert!(*wake.state.0.try_lock().unwrap());
        fail_notified(&failure,&wake,"later failure must not replace first".into());
        assert_eq!(failure.lock().unwrap().as_deref(),Some(expected));
        if let Some(receiver)=receiver {
            for (sequence,address) in queued.iter().enumerate() {
                assert_eq!(observe_frame(receiver.try_recv().ok().expect("queued frame lost"),sequence as u64),*address);
            }
            assert_eq!(queued.len(),CAPACITY);
            assert!(matches!(receiver.try_recv(),Err(mpsc::TryRecvError::Empty)));
        }
    }
    struct ReturnedFrame<'a> {
        frame:Option<Event>,failure:&'a Mutex<Option<String>>,wake:&'a WakeSignal,
        observed:&'a Mutex<Option<(Option<String>,bool)>>,
    }
    impl Drop for ReturnedFrame<'_> {
        fn drop(&mut self) {
            // Destroy the actual raw payload before observing the unchanged
            // original emit's first-failure and wake boundary.
            drop(self.frame.take());
            let first=self.failure.try_lock().expect("failure lock held while releasing returned frame").clone();
            let pending=*self.wake.state.0.try_lock().expect("wake lock held while releasing returned frame");
            *self.observed.lock().unwrap()=Some((first,pending));
        }
    }
    fn rejected_release(disconnected:bool,prior:Option<&str>) {
        let failure=Mutex::new(prior.map(str::to_owned));let wake=WakeSignal::default();let observed=Mutex::new(None);
        let (sender,receiver)=mpsc::sync_channel::<Option<ReturnedFrame<'_>>>(CAPACITY);
        let receiver=if disconnected {drop(receiver);None}else{Some(receiver)};
        if !disconnected {for _ in 0..CAPACITY {sender.try_send(None).ok().unwrap();}}
        let (event,_)=frame(CAPACITY as u64);let mut scheduled=0;
        let returned=ReturnedFrame{frame:Some(event),failure:&failure,wake:&wake,observed:&observed};
        assert!(!emit_cooperatively(&sender,&failure,&wake,Some(returned),||scheduled+=1));
        assert_eq!(scheduled,0);assert_eq!(*observed.lock().unwrap(),Some((prior.map(str::to_owned),false)));
        let reason=if disconnected {"native ingress receiver disconnected; owner incomplete"}else{"native ingress full; owner incomplete"};
        assert_eq!(failure.try_lock().unwrap().as_deref(),Some(prior.unwrap_or(reason)));
        assert!(*wake.state.0.try_lock().unwrap());drop(receiver);
    }
    #[test]
    fn full_and_disconnected_preserve_release_first_failure_wake_without_scheduling() {
        assert_eq!(CAPACITY,4096);
        for disconnected in [false,true] {for prior in [None,Some("original first error")] {
            rejected_event(disconnected,prior);rejected_release(disconnected,prior);
        }}
    }

    #[test]
    fn real_yield_wrapper_preserves_bounded_threaded_raw_fifo_and_cleanup() {
        assert_eq!(CAPACITY,4096);
        for (value,enabled) in [(None,false),(Some(std::ffi::OsStr::new("v1")),true)] {
            let cooperate=ingress_cooperate(value);assert_eq!(cooperate,enabled);
            let (sender,receiver)=mpsc::sync_channel::<Event>(CAPACITY);
            let failure=Arc::new(Mutex::new(None));let worker_failure=failure.clone();
            let wake=WakeSignal::default();let worker_wake=wake.clone();
            let producer=thread::spawn(move||{
                let mut addresses=Vec::new();let mut callbacks=0;
                for sequence in 0..FRAMES as u64 {
                    let (event,address)=frame(sequence);addresses.push(address);
                    assert!(emit_cooperatively(&sender,&worker_failure,&worker_wake,event,||{
                        yield_raw_ingress(cooperate);callbacks+=1;
                    }));
                }
                // Dropping this sole sender closes the bounded receiver.
                (addresses,callbacks)
            });
            let consumer=thread::spawn(move||{
                let mut addresses=Vec::new();
                while let Ok(event)=receiver.recv() {addresses.push(observe_frame(event,addresses.len() as u64));}
                addresses
            });
            let sent=producer.join();let received=consumer.join();
            let (expected,callbacks)=sent.expect("raw producer panicked");let actual=received.expect("raw consumer panicked");
            assert_eq!(actual,expected);assert_eq!(actual.len(),FRAMES);assert_eq!(callbacks,FRAMES);
            assert!(failure.try_lock().unwrap().is_none());assert!(*wake.state.0.try_lock().unwrap());
            // Both real threads have joined; no timing or capacity claim follows
            // from exercising the actual std yield wrapper in either mode.
        }
    }
}

#[cfg(test)]
mod ingress_credit_v2_tests {
    use super::*;
    use super::handoff_tests::empty_runtime;
    use std::net::TcpListener;
    use tungstenite::Message;

    const LIMIT:Duration=Duration::from_secs(10);
    const WALL:i64=1_800_000_000_000;
    fn payload(sequence:u64)->Value {
        json!({"s":"ETHUSDT","u":sequence,"E":WALL,"T":WALL-1,
            "b":"999999999.99999999","a":"1000000000.00000000",
            "B":"0.00000001","A":"1.23456789","extra":[null,true,"verbatim"]})
    }
    fn frame(sequence:u64)->Event {
        Event::Frame{route:"credit-route".into(),token:"test-credit-connection".into(),generation:7,
            payload:payload(sequence),clock:Clock{wall_ms:WALL+sequence as i64,monotonic_ms:sequence as i64}}
    }
    fn ingress(pool:&Arc<IngressCredits>,stop:Arc<AtomicBool>)->WorkerIngress {
        WorkerIngress{mode:IngressMode::V2,credits:Some(pool.clone()),stop}
    }
    fn free(pool:&IngressCredits)->usize {pool.state.lock().unwrap().free}
    fn await_pressure(pool:&IngressCredits) {
        let until=Instant::now()+LIMIT;let mut state=pool.state.lock().unwrap();
        while state.waiting==0 {
            let remaining=until.saturating_duration_since(Instant::now());assert!(!remaining.is_zero(),"no genuine credit wait");
            state=pool.ready.wait_timeout(state,remaining).unwrap().0;
        }
        assert_eq!(state.free,0);
    }
    fn await_finished(runtime:&Runtime,route:&str) {
        let until=Instant::now()+LIMIT;
        while runtime.retiring[route].join.as_ref().is_some_and(|join|!join.is_finished()) {
            assert!(Instant::now()<until,"worker failed bounded cooperative exit");thread::yield_now();
        }
    }
    fn configured(pool:&Arc<IngressCredits>)->Runtime {
        let mut runtime=empty_runtime();runtime.enable_handoff().unwrap();
        runtime.ingress_mode=IngressMode::V2;runtime.ingress_credits=Some(pool.clone());runtime
    }

    #[test]
    fn venue_batch_captures_every_row_and_accounts_for_benign_stop_tail() {
        for fatal in [false, true] {
            let pool = IngressCredits::new();
            let mut runtime = configured(&pool);
            let stop = Arc::new(AtomicBool::new(false));
            let permits: Vec<_> = (1..CAPACITY).map(|_| {
                pool.reserve(&stop, &runtime.failure, None).ok().unwrap()
            }).collect();
            let ingress = ingress(&pool, stop.clone());
            let (sender, failure, wake, received) = (
                runtime.sender.clone(), runtime.failure.clone(),
                runtime.control.wake.clone(), runtime.received.clone(),
            );
            let join = thread::spawn(move || {
                let clock = Clock {wall_ms:WALL, monotonic_ms:1};
                let quotes = (1..=3).map(|sequence| SpotQuote {
                    symbol:"ETHUSDT".into(), bid:"1".into(), ask:"2".into(),
                    source_ms:WALL, receive_ms:WALL, sequence:Some(sequence),
                }).collect();
                offer_venue_batch(quotes, &received, |quote,batch_has_more| ingress.offer(&sender, &failure, &wake,
                    Event::VenueQuote {route:"okx".into(),token:"test-okx".into(),generation:2,quote,clock,batch_has_more}))
            });
            await_pressure(&pool);
            assert_eq!(runtime.received.load(Ordering::Acquire), 3);
            if fatal {
                fail(&runtime.failure, "original batch ingress failure".into());
            }
            stop.store(true, Ordering::Release);
            let outcome = join.join().unwrap();
            if fatal {
                assert_eq!(outcome.unwrap_err(), "ingress failed");
                assert_eq!(runtime.failure.lock().unwrap().as_deref(), Some("original batch ingress failure"));
                assert_eq!(runtime.retired_frames, 0);
            } else {
                let canceled_post_ack = outcome.unwrap();
                assert_eq!(canceled_post_ack, 2);
                assert!(runtime.collect_worker_exit(WorkerExit {canceled_post_ack}, None).is_err());
                runtime.collect_worker_exit(WorkerExit {canceled_post_ack}, Some(SpotVenue::Okx)).unwrap();
                assert_eq!(runtime.retired_frames, 2);
            }
            let Event::VenueQuote {quote,clock,batch_has_more,..} = runtime.receiver.try_recv().unwrap().release_credit() else {
                panic!("expected the first batch quote");
            };
            assert_eq!(quote.sequence, Some(1));
            assert_eq!((clock.wall_ms,clock.monotonic_ms,batch_has_more),(WALL,1,true));
            assert!(matches!(runtime.receiver.try_recv(), Err(mpsc::TryRecvError::Empty)));
            drop(permits);
            assert_eq!(free(&pool), CAPACITY);
        }
    }

    #[test]
    fn exact_v2_selection_requires_configured_constructor_without_environment_mutation() {
        for configured in [false,true] {
            assert_eq!(select_ingress_mode(None,configured).unwrap(),IngressMode::Off);
            assert_eq!(select_ingress_mode(Some(std::ffi::OsStr::new("v1")),configured).unwrap(),IngressMode::V1);
            for value in ["","V2","v2 "," v2","v2\n","v2\0","true"] {
                assert_eq!(select_ingress_mode(Some(std::ffi::OsStr::new(value)),configured).unwrap(),IngressMode::Off);
            }
        }
        assert!(select_ingress_mode(Some(std::ffi::OsStr::new("v2")),false).is_err());
        assert_eq!(select_ingress_mode(Some(std::ffi::OsStr::new("v2")),true).unwrap(),IngressMode::V2);
        #[cfg(unix)] {
            use std::os::unix::ffi::OsStrExt;
            assert_eq!(select_ingress_mode(Some(std::ffi::OsStr::from_bytes(b"v2\xff")),true).unwrap(),IngressMode::Off);
        }
        let runtime=empty_runtime();let settings=runtime.settings.clone();
        assert!(Runtime::start_with_ingress_mode(settings,LifecyclePolicy::default(),false,IngressMode::V2).is_err());
        assert!(runtime.ingress_credits.is_none());
    }

    #[test]
    fn real_4096_credits_preserve_fifo_identity_and_return_on_both_pops_and_drop() {
        assert_eq!(CAPACITY,4096);
        let pool=IngressCredits::new();let other=IngressCredits::new();let runtime=configured(&pool);
        let stop=Arc::new(AtomicBool::new(false));let producer=ingress(&pool,stop.clone());
        let reserved=other.reserve(&stop,&runtime.failure,None).ok().unwrap();
        assert_eq!((free(&pool),free(&other)),(CAPACITY,CAPACITY-1));
        drop(reserved);assert_eq!(free(&other),CAPACITY);
        let mut addresses=Vec::with_capacity(CAPACITY);
        for sequence in 0..CAPACITY as u64 {
            let event=frame(sequence);let Event::Frame{payload,..}=&event else{unreachable!()};
            addresses.push(payload["b"].as_str().unwrap().as_ptr() as usize);
            assert_eq!(producer.offer(&runtime.sender,&runtime.failure,&runtime.control.wake,event),OfferResult::Sent);
        }
        assert_eq!(free(&pool)+CAPACITY,CAPACITY);
        let sender=runtime.sender.clone();let failure=runtime.failure.clone();let wake=runtime.control.wake.clone();
        let (sent,finished)=mpsc::sync_channel(1);
        let join=thread::spawn(move||{let result=producer.offer(&sender,&failure,&wake,frame(CAPACITY as u64));sent.send(result).unwrap();});
        await_pressure(&pool);assert!(matches!(finished.try_recv(),Err(mpsc::TryRecvError::Empty)));
        for (sequence,address) in addresses.into_iter().enumerate() {
            let event=runtime.receive_event(Duration::ZERO).unwrap().unwrap();
            if sequence==0 {assert_eq!(finished.recv_timeout(LIMIT).unwrap(),OfferResult::Sent);}
            assert!(!event.has_credit());
            let Event::Frame{route,token,generation,payload:actual,clock}=event else{panic!("expected frame")};
            assert_eq!((route.as_str(),token.as_str(),generation),("credit-route","test-credit-connection",7));
            assert_eq!(actual,payload(sequence as u64));assert_eq!(actual["b"].as_str().unwrap().as_ptr() as usize,address);
            assert_eq!((clock.wall_ms,clock.monotonic_ms),(WALL+sequence as i64,sequence as i64));
        }
        join.join().unwrap();drop(runtime.receive_event(Duration::ZERO).unwrap().unwrap());assert_eq!(free(&pool),CAPACITY);
        // The explicit observation is after the first empty try_recv, forcing
        // the other successful receive path without a scheduling-time guess.
        *runtime.control.wake.state.0.lock().unwrap()=false;
        let (reached,observed)=mpsc::sync_channel(1);*pool.before_receive_wait.lock().unwrap()=Some(reached);
        let sender=runtime.sender.clone();let failure=runtime.failure.clone();let wake=runtime.control.wake.clone();let producer=ingress(&pool,stop.clone());
        let join=thread::spawn(move||{observed.recv_timeout(LIMIT).unwrap();assert_eq!(producer.offer(&sender,&failure,&wake,frame(99)),OfferResult::Sent);});
        let event=runtime.receive_event(LIMIT).unwrap().unwrap();join.join().unwrap();assert_eq!(free(&pool),CAPACITY);
        assert!(!event.has_credit());assert!(matches!(event,Event::Frame{..}));drop(event);
        let permit=pool.reserve(&stop,&runtime.failure,None).ok().unwrap();assert_eq!(free(&pool)+1,CAPACITY);
        let unsent=frame(100).with_credit(permit);drop(unsent);assert_eq!(free(&pool),CAPACITY);
        let producer=ingress(&pool,stop);let (sender,receiver)=mpsc::sync_channel(CAPACITY);
        for sequence in 0..CAPACITY as u64 {assert_eq!(producer.offer(&sender,&runtime.failure,&runtime.control.wake,frame(sequence)),OfferResult::Sent);}
        assert_eq!(free(&pool),0);drop(receiver);assert_eq!(free(&pool),CAPACITY);drop(sender);
        drop(runtime);assert_eq!((free(&pool),free(&other)),(CAPACITY,CAPACITY));
    }

    #[test]
    fn v2_actual_full_disconnected_and_wait_cancellation_preserve_first_fatal() {
        for disconnected in [false,true] {
            let pool=IngressCredits::new();let stop=Arc::new(AtomicBool::new(false));let producer=ingress(&pool,stop);
            let (sender,receiver)=mpsc::sync_channel(CAPACITY);let failure=Mutex::new(None);let wake=WakeSignal::default();
            let receiver=if disconnected{drop(receiver);None}else{Some(receiver)};
            // Deliberately inconsistent channel: these existing legacy offers
            // own no credits. A real try_send Full still fails, never retries.
            if !disconnected {for sequence in 0..CAPACITY as u64 {assert!(emit(&sender,&failure,frame(sequence)));}}
            assert_eq!(producer.offer(&sender,&failure,&wake,frame(9000)),OfferResult::Failed);
            let expected=if disconnected{"native ingress receiver disconnected; owner incomplete"}else{"native ingress full; owner incomplete"};
            assert_eq!(failure.lock().unwrap().as_deref(),Some(expected));assert_eq!(free(&pool),CAPACITY);assert!(*wake.state.0.lock().unwrap());
            fail_notified(&failure,&wake,"later".into());assert_eq!(failure.lock().unwrap().as_deref(),Some(expected));drop(receiver);
        }
        for fatal in [false,true] {
            let pool=IngressCredits::new();let stop=Arc::new(AtomicBool::new(false));let failure=Arc::new(Mutex::new(None));
            let permits:Vec<_>=(0..CAPACITY).map(|_|pool.reserve(&stop,&failure,None).ok().unwrap()).collect();
            let producer=ingress(&pool,stop.clone());let worker_failure=failure.clone();let (sender,receiver)=mpsc::sync_channel(CAPACITY);let wake=WakeSignal::default();
            let join=thread::spawn(move||producer.offer(&sender,&worker_failure,&wake,frame(1)));
            await_pressure(&pool);
            if fatal {fail(&failure,"original first-fatal".into());}stop.store(true,Ordering::Release);
            assert_eq!(join.join().unwrap(),if fatal{OfferResult::Failed}else{OfferResult::Stopped});
            assert!(matches!(receiver.try_recv(),Err(mpsc::TryRecvError::Disconnected)));assert_eq!(free(&pool),0);
            drop(permits);assert_eq!(free(&pool),CAPACITY);
            assert_eq!(failure.lock().unwrap().as_deref(),if fatal{Some("original first-fatal")}else{None});
        }
    }

    struct Fixture {endpoint:Endpoint,ack:SyncSender<()>,go:SyncSender<()>,following:SyncSender<()>,sent:Receiver<()>,done:SyncSender<()>,join:Option<JoinHandle<Result<()>>>}
    impl Fixture {
        fn start(frames:u64,pre_ack:bool)->Self {
            Self::start_with_pause(frames, pre_ack, false)
        }

        fn start_with_pause(frames:u64,pre_ack:bool,pause_after_first:bool)->Self {
            let listener=TcpListener::bind("127.0.0.1:0").unwrap();let address=listener.local_addr().unwrap();listener.set_nonblocking(true).unwrap();
            let (ack,acknowledged)=mpsc::sync_channel(1);let (go,started)=mpsc::sync_channel(1);let (sent,finished)=mpsc::sync_channel(1);let (done,closed)=mpsc::sync_channel(1);
            let (following, resume)=mpsc::sync_channel(1);
            let join=thread::spawn(move||->Result<()> {
                let until=Instant::now()+LIMIT;
                let stream=loop {match listener.accept(){Ok((stream,_))=>break stream,Err(error)if error.kind()==std::io::ErrorKind::WouldBlock=>{if Instant::now()>=until{return Err("credit fixture accept timeout".into())}thread::yield_now();},Err(error)=>return Err(error.to_string())}};
                stream.set_read_timeout(Some(LIMIT)).map_err(|error|error.to_string())?;stream.set_write_timeout(Some(LIMIT)).map_err(|error|error.to_string())?;
                let mut socket=tungstenite::accept(stream).map_err(|error|error.to_string())?;
                let request:Value=serde_json::from_str(&socket.read().map_err(|error|error.to_string())?.into_text().map_err(|error|error.to_string())?).map_err(|error|error.to_string())?;
                if request["params"]!=json!(["ethusdt@bookTicker"]){return Err("credit fixture subscription mismatch".into())}
                acknowledged.recv_timeout(LIMIT).map_err(|error|error.to_string())?;
                if pre_ack {socket.send(Message::Text(payload(0).to_string().into())).map_err(|error|error.to_string())?;}
                socket.send(Message::Text(json!({"id":request["id"],"result":null}).to_string().into())).map_err(|error|error.to_string())?;
                started.recv_timeout(LIMIT).map_err(|error|error.to_string())?;
                for sequence in (if pre_ack{1}else{0})..frames {
                    if pause_after_first && sequence == 1 { resume.recv_timeout(LIMIT).map_err(|error|error.to_string())?; }
                    socket.send(Message::Text(payload(sequence).to_string().into())).map_err(|error|error.to_string())?;
                }
                sent.send(()).map_err(|error|error.to_string())?;let _=closed.recv_timeout(LIMIT);Ok(())
            });
            Self{endpoint:Endpoint{address,uri:format!("ws://localhost:{}/credit",address.port())},ack,go,following,sent:finished,done,join:Some(join)}
        }
        fn settings(&self)->Settings {Settings{session:"credit-fixture".into(),mode:"single".into(),perpetual:self.endpoint.clone(),spot:self.endpoint.clone(),perpetual_symbols:vec!["ETHUSDT".into()],spot_symbols:vec![],fixture_wall_ms:Some(WALL),fixture_backup_endpoint:None}}
        fn finish(&mut self)->Result<()> {let _=self.ack.try_send(());let _=self.go.try_send(());let _=self.following.try_send(());let _=self.done.try_send(());self.join.take().ok_or("fixture already joined")?.join().map_err(|_|"credit fixture panicked")?}
    }
    impl Drop for Fixture {fn drop(&mut self){if self.join.is_some(){let _=self.finish();}}}
    fn start(fixture:&Fixture)->Runtime {let runtime=Runtime::start_with_ingress_mode(fixture.settings(),LifecyclePolicy::default(),true,IngressMode::V2).unwrap();fixture.ack.send(()).unwrap();runtime}
    fn await_ack(runtime:&mut Runtime) {
        let until=Instant::now()+LIMIT;
        while runtime.workers.values().any(|worker|!worker.connected){assert!(Instant::now()<until);runtime.recv(Duration::from_millis(20)).unwrap();}
    }
    fn drain_retired(runtime:&mut Runtime,count:usize) {
        for _ in 0..count {assert!(runtime.recv(Duration::ZERO).unwrap().is_empty());}
    }

    fn start_spot(perp: &Fixture, spot: &Fixture) -> (Runtime, String, Arc<SpotIngress>) {
        let mut settings = perp.settings();
        settings.spot = spot.endpoint.clone();
        settings.spot_symbols = settings.perpetual_symbols.clone();
        let mut runtime = Runtime::start_with_modes(settings, LifecyclePolicy::default(), true, IngressMode::V2, SpotProbe::Ingress).unwrap();
        perp.ack.send(()).unwrap();
        spot.ack.send(()).unwrap();
        await_ack(&mut runtime);
        perp.go.send(()).unwrap();
        perp.sent.recv_timeout(LIMIT).unwrap();
        let (route, worker) = runtime.workers.iter().find(|(_, worker)| worker.spot).unwrap();
        let route = route.clone();
        let probe = worker.spot_ingress.clone().expect("original spot ACK installs the probe");
        (runtime, route, probe)
    }

    fn hold_credits(runtime: &Runtime, pool: &Arc<IngressCredits>) -> Vec<IngressPermit> {
        assert_eq!(CAPACITY, 4096);
        assert_eq!(free(pool), CAPACITY);
        let stop = AtomicBool::new(false);
        (0..CAPACITY).map(|_| pool.reserve(&stop, &runtime.failure, None).ok().unwrap()).collect()
    }

    fn next_spot_frame(runtime: &mut Runtime, probe: &SpotIngress, sequence: u64) {
        let until = Instant::now() + LIMIT;
        loop {
            assert!(Instant::now() < until, "real spot frame did not drain");
            let rows = runtime.recv(Duration::from_millis(20)).unwrap();
            let Some(row) = rows.first() else { continue };
            assert_eq!(rows.len(), 1);
            assert_eq!(row["type"], "spot_raw");
            assert_eq!(row["token"], probe.token);
            assert_eq!(row["generation"], probe.generation);
            assert_eq!(row["payload"], payload(sequence));
            break;
        }
    }

    #[test]
    fn actual_spot_receive_stages_bind_duplicate_sequence_and_reconnect() {
        let mut perp = Fixture::start(0, false);
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        listener.set_nonblocking(true).unwrap();
        let (commands, next) = mpsc::sync_channel(1);
        let spot = thread::spawn(move || {
            for generation in 1..=2 {
                let end = Instant::now() + LIMIT;
                let stream = loop {
                    match listener.accept() {
                        Ok((stream, _)) => break stream,
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            assert!(Instant::now() < end, "missing real spot connection");
                            thread::sleep(Duration::from_millis(5));
                        }
                        Err(error) => panic!("spot fixture accept: {error}"),
                    }
                };
                stream.set_read_timeout(Some(LIMIT)).unwrap();
                stream.set_write_timeout(Some(LIMIT)).unwrap();
                let mut socket = tungstenite::accept(stream).unwrap();
                let request: Value = serde_json::from_str(&socket.read().unwrap().into_text().unwrap()).unwrap();
                assert_eq!(request["id"], generation);
                assert_eq!(request["params"], json!(["ethusdt@bookTicker"]));
                if generation == 1 {
                    socket.send(Message::Text(payload(0).to_string().into())).unwrap();
                }
                socket.send(Message::Text(json!({"id": generation, "result": null}).to_string().into())).unwrap();
                loop {
                    match next.recv_timeout(LIMIT).unwrap() {
                        "quote" => socket.send(Message::Text(payload(0).to_string().into())).unwrap(),
                        "bad" => {
                            socket.send(Message::Text("invalid json".into())).unwrap();
                            break;
                        }
                        "stop" => return,
                        _ => panic!("invalid spot fixture command"),
                    }
                }
            }
        });

        let mut settings = perp.settings();
        settings.spot = Endpoint { address, uri: format!("ws://localhost:{}/receive", address.port()) };
        settings.spot_symbols = settings.perpetual_symbols.clone();
        let mut runtime = Runtime::start_with_modes(settings, LifecyclePolicy::default(), true,
            IngressMode::V2, SpotProbe::ReceiveStages).unwrap();
        perp.ack.send(()).unwrap();
        await_ack(&mut runtime);
        perp.go.send(()).unwrap();
        perp.sent.recv_timeout(LIMIT).unwrap();
        let control = runtime.control();
        let current = |runtime: &Runtime| runtime.workers.values().find(|worker| worker.spot).unwrap()
            .spot_ingress.clone().unwrap();
        let first = current(&runtime);
        let sample = |probe: &SpotIngress| control.spot_receive_stages(&probe.token, probe.generation, "ETHUSDT");
        let after_dequeue = |probe: &SpotIngress| {
            let end = Instant::now() + Duration::from_secs(1);
            loop {
                if let Some(row) = sample(probe) { return row; }
                assert!(Instant::now() < end, "dequeued frame has no stable stages");
                thread::sleep(Duration::from_millis(1));
            }
        };
        let assert_dequeued = |row: SpotReceiveStages| {
            assert!(row.offered);
            assert!(row.text_received_ns / 1_000_000 <= row.worker_observed_ms as u64);
            assert!(row.worker_observed_ms <= row.dequeued_at_ms.unwrap());
            row
        };
        next_spot_frame(&mut runtime, &first, 0);
        let initial = assert_dequeued(after_dequeue(&first));
        assert_eq!((initial.decoded, initial.gaps_over_one_second), (1, 0));
        assert_eq!(control.spot_decode_failures(), Some(0));

        thread::sleep(Duration::from_millis(1050));
        commands.send("quote").unwrap();
        next_spot_frame(&mut runtime, &first, 0);
        let later = assert_dequeued(after_dequeue(&first));
        assert_eq!((later.decoded, later.gaps_over_one_second), (2, 1));
        assert_eq!(later.max_text_gap_ns, later.text_received_ns - initial.text_received_ns);
        assert!(later.max_text_gap_ns > 1_000_000_000);

        commands.send("bad").unwrap();
        let end = Instant::now() + LIMIT;
        while runtime.workers.values().any(|worker| worker.spot && worker.generation != 2) {
            assert!(Instant::now() < end, "real spot generation did not reconnect");
            runtime.recv(Duration::from_millis(20)).unwrap();
        }
        let second = current(&runtime);
        assert_eq!((second.worker_instance, second.generation), (first.worker_instance, first.generation + 1));
        assert!(sample(&first).is_none());
        assert!(sample(&second).is_none());
        assert_eq!(control.spot_decode_failures(), Some(1));
        commands.send("quote").unwrap();
        next_spot_frame(&mut runtime, &second, 0);
        let fresh = assert_dequeued(after_dequeue(&second));
        assert_eq!((fresh.decoded, fresh.gaps_over_one_second), (1, 0));
        assert_eq!(fresh.generation, second.generation);
        assert_eq!((runtime.received.load(Ordering::Acquire), runtime.supplied, runtime.retired_frames), (3, 3, 0));

        runtime.request_handoff_shutdown().unwrap();
        for route in runtime.retiring.keys() { await_finished(&runtime, route); }
        assert!(runtime.handoff_shutdown_ready().unwrap());
        runtime.shutdown().unwrap();
        assert!(sample(&second).is_none());
        commands.send("stop").unwrap();
        spot.join().unwrap();
        perp.finish().unwrap();
    }

    #[test]
    fn spot_receive_stages_never_joins_a_new_text_to_an_old_dequeue() {
        let mut state = SpotReceiveState::new(&["ETHUSDT".into()]);
        state.capture(&payload(0), Some(100), 0);
        state.offered = 1;
        state.capture(&payload(0), Some(1_100_000), 1);
        state.dequeue("ETHUSDT", 2);
        let pending = state.sample(3, 4, "ETHUSDT").unwrap();
        assert_eq!((pending.text_received_ns, pending.offered, pending.dequeued_at_ms), (1_100_000, false, None));

        // The channel receiver can win the race with offer's return.
        state.dequeue("ETHUSDT", 3);
        let complete = state.sample(3, 4, "ETHUSDT").unwrap();
        assert_eq!((complete.offered, complete.dequeued_at_ms), (true, Some(3)));
    }

    #[test]
    fn actual_spot_credit_wait_repeated_park_then_grant_preserves_fifo() {
        let mut perp = Fixture::start(0, false);
        let mut spot = Fixture::start_with_pause(2, false, true);
        let (mut runtime, _, probe) = start_spot(&perp, &spot);
        let control = runtime.control();
        let sample = || control.spot_credit_wait(&probe.token, probe.generation).unwrap();
        assert_eq!(sample(), SpotCreditWaitSummary { granted: 0, stopped: 0, failed: 0,
            completed_total_ns: 0, completed_max_ns: 0, in_flight_age_ns: None });
        assert!(control.spot_credit_wait("wrong-token", probe.generation).is_none());
        assert!(control.spot_credit_wait(&probe.token, probe.generation + 1).is_none());
        let pool = runtime.ingress_credits.clone().unwrap();
        let mut held = hold_credits(&runtime, &pool);
        spot.go.send(()).unwrap();
        await_pressure(&pool);
        let age = sample().in_flight_age_ns.unwrap();
        {
            let until = Instant::now() + LIMIT;
            let mut state = pool.state.lock().unwrap();
            let target = state.parks + 2;
            while state.parks < target {
                assert!(Instant::now() < until, "worker did not re-park with zero free credits");
                pool.ready.notify_all();
                state = pool.ready.wait_timeout(state, until.saturating_duration_since(Instant::now())).unwrap().0;
            }
            assert_eq!((state.free, state.waiting), (0, 1));
        }
        let active = sample();
        assert!(active.in_flight_age_ns.unwrap() >= age);
        assert_eq!((active.granted, active.stopped, active.failed, active.completed_total_ns, active.completed_max_ns), (0, 0, 0, 0, 0));
        assert_eq!((runtime.received.load(Ordering::Acquire), runtime.supplied), (1, 0));
        drop(held.pop().unwrap());
        next_spot_frame(&mut runtime, &probe, 0);
        let granted = sample();
        assert_eq!((granted.granted, granted.stopped, granted.failed, granted.in_flight_age_ns), (1, 0, 0, None));
        assert!(granted.completed_total_ns >= active.in_flight_age_ns.unwrap());
        assert!(granted.completed_total_ns > 0);
        assert_eq!(granted.completed_total_ns, granted.completed_max_ns);
        // The first dequeue returned the one released permit for the next frame.
        spot.following.send(()).unwrap();
        next_spot_frame(&mut runtime, &probe, 1);
        spot.sent.recv_timeout(LIMIT).unwrap();
        assert_eq!(sample(), granted);
        assert_eq!((runtime.received.load(Ordering::Acquire), runtime.supplied, runtime.retired_frames), (2, 2, 0));
        drop(held);
        runtime.request_handoff_shutdown().unwrap();
        for route in runtime.retiring.keys() { await_finished(&runtime, route); }
        assert!(runtime.handoff_shutdown_ready().unwrap());
        runtime.shutdown().unwrap();
        assert_eq!(free(&pool), CAPACITY);
        assert!(control.spot_credit_wait(&probe.token, probe.generation).is_none());
        spot.finish().unwrap();
        perp.finish().unwrap();
    }

    #[test]
    fn actual_spot_credit_wait_classifies_stop_and_failure() {
        for fatal in [false, true] {
            let mut perp = Fixture::start(0, false);
            let mut spot = Fixture::start(1, false);
            let (mut runtime, route, probe) = start_spot(&perp, &spot);
            let pool = runtime.ingress_credits.clone().unwrap();
            let held = hold_credits(&runtime, &pool);
            spot.go.send(()).unwrap();
            await_pressure(&pool);
            let active = probe.credit_wait.as_ref().unwrap().snapshot().unwrap();
            assert!(active.in_flight_age_ns.is_some());
            assert_eq!(runtime.received.load(Ordering::Acquire), 1);
            if fatal { fail(&runtime.failure, "original spot credit failure".into()); }
            runtime.detach_handoff(vec![route.clone()]).unwrap();
            await_finished(&runtime, &route);
            runtime.reap_ready().unwrap();
            let completed = probe.credit_wait.as_ref().unwrap().snapshot().unwrap();
            assert_eq!((completed.granted, completed.stopped, completed.failed, completed.in_flight_age_ns),
                (0, if fatal { 0 } else { 1 }, if fatal { 1 } else { 0 }, None));
            assert!(completed.completed_total_ns >= active.in_flight_age_ns.unwrap());
            assert!(completed.completed_total_ns > 0);
            assert_eq!(completed.completed_total_ns, completed.completed_max_ns);
            assert!(runtime.control.spot_credit_wait(&probe.token, probe.generation).is_none());
            assert_eq!((runtime.supplied, runtime.retired_frames), (0, if fatal { 0 } else { 1 }));
            assert_eq!(free(&pool), 0);
            drop(held);
            runtime.request_handoff_shutdown().unwrap();
            for route in runtime.retiring.keys() { await_finished(&runtime, route); }
            if fatal {
                assert_eq!(runtime.handoff_shutdown_ready().unwrap_err(), "original spot credit failure");
                assert_eq!(runtime.failure.lock().unwrap().as_deref(), Some("original spot credit failure"));
            } else {
                assert!(runtime.handoff_shutdown_ready().unwrap());
                assert!(runtime.failure.lock().unwrap().is_none());
            }
            runtime.shutdown().unwrap();
            assert_eq!(free(&pool), CAPACITY);
            spot.sent.recv_timeout(LIMIT).unwrap();
            spot.finish().unwrap();
            perp.finish().unwrap();
        }
    }

    #[test]
    fn spot_credit_wait_snapshot_rejects_unstable_transition() {
        let origin = Instant::now();
        let wait = SpotCreditWait::new(origin);
        wait.write(|| assert!(wait.snapshot().is_none()));
        assert!(wait.snapshot().unwrap().in_flight_age_ns.is_none());
        wait.version.store(u64::MAX - 1, Ordering::SeqCst);
        wait.write(|| panic!("exhausted diagnostic must stay unavailable"));
        assert!(wait.snapshot().is_none());
    }

    #[test]
    fn actual_spot_credit_interval_uses_acknowledged_worker_and_runtime_clock() {
        let mut perp = Fixture::start(0, false);
        let mut spot = Fixture::start(1, false);
        let (mut runtime, route, probe) = start_spot(&perp, &spot);
        let control = runtime.control();
        let sample = || control.spot_credit_interval(&probe.token, probe.generation).unwrap();
        let captured = control.clock();
        let (first_wait, first) = sample();
        assert_eq!(first.worker_instance, runtime.next);
        assert_eq!(first.generation, runtime.workers[&route].generation);
        assert!(first.sampled_at_ns / 1_000_000 >= captured.monotonic_ms as u64);
        assert_eq!(json!(first), json!({
            "worker_instance": runtime.next, "generation": probe.generation,
            "sampled_at_ns": first.sampled_at_ns,
        }));
        assert_eq!(control.spot_credit_wait(&probe.token, probe.generation), Some(first_wait));
        assert!(control.spot_credit_interval("stale-token", probe.generation).is_none());
        assert!(control.spot_credit_interval(&probe.token, probe.generation + 1).is_none());

        let pool = runtime.ingress_credits.clone().unwrap();
        let mut held = hold_credits(&runtime, &pool);
        spot.go.send(()).unwrap();
        await_pressure(&pool);
        let (active_wait, active) = sample();
        let started = probe.credit_wait.as_ref().unwrap().started.load(Ordering::SeqCst);
        assert_eq!(active_wait.in_flight_age_ns, active.sampled_at_ns.checked_sub(started));
        assert!(active_wait.in_flight_age_ns.is_some());
        assert!(active.sampled_at_ns > first.sampled_at_ns);
        drop(held.pop().unwrap());
        next_spot_frame(&mut runtime, &probe, 0);
        spot.sent.recv_timeout(LIMIT).unwrap();
        let (completed_wait, completed) = sample();
        assert_eq!((completed.worker_instance, completed.generation), (first.worker_instance, first.generation));
        assert!(completed.sampled_at_ns > active.sampled_at_ns);
        assert!(u128::from(completed.sampled_at_ns) <= control.origin.elapsed().as_nanos());
        assert_eq!((completed_wait.granted, completed_wait.in_flight_age_ns), (1, None));
        assert_eq!(control.spot_credit_wait(&probe.token, probe.generation), Some(completed_wait));

        drop(held);
        runtime.request_handoff_shutdown().unwrap();
        for route in runtime.retiring.keys() { await_finished(&runtime, route); }
        assert!(runtime.handoff_shutdown_ready().unwrap());
        runtime.shutdown().unwrap();
        assert!(control.spot_credit_interval(&probe.token, probe.generation).is_none());
        spot.finish().unwrap();
        perp.finish().unwrap();
    }

    #[test]
    fn spot_credit_interval_distinguishes_reused_generation_and_rejects_unstable_snapshot() {
        let mut control = Control::default();
        let first = Arc::new(SpotIngress::for_generation("first", 2, 1, &[], Some(control.origin)));
        let replacement = Arc::new(SpotIngress::for_generation("replacement", 3, 1, &[], Some(control.origin)));
        let registration = Arc::new(Mutex::new(Some(first)));
        control.spot_ingress = Some(registration.clone());
        let (_, first) = control.spot_credit_interval("first", 1).unwrap();
        *registration.lock().unwrap() = Some(replacement.clone());
        let (_, second) = control.spot_credit_interval("replacement", 1).unwrap();
        assert_eq!(first.generation, second.generation);
        assert_ne!(first.worker_instance, second.worker_instance);
        assert!(control.spot_credit_interval("first", 1).is_none());
        replacement.credit_wait.as_ref().unwrap().write(|| {
            assert!(control.spot_credit_interval("replacement", 1).is_none());
        });
        assert!(control.spot_credit_interval("replacement", 1).is_some());
        *registration.lock().unwrap() = Some(Arc::new(SpotIngress::for_generation("v1", 4, 1, &[], None)));
        assert!(control.spot_credit_interval("v1", 1).is_none());
    }

    #[test]
    fn configured_loopback_5000_actual_frames_resume_after_pressure_with_exact_capture_clocks() {
        let mut fixture=Fixture::start(5000,false);let mut runtime=start(&fixture);await_ack(&mut runtime);
        let pool=runtime.ingress_credits.clone().unwrap();fixture.go.send(()).unwrap();await_pressure(&pool);
        assert_eq!(runtime.received.load(Ordering::Acquire),CAPACITY as u64+1);assert_eq!((runtime.supplied,runtime.retired_frames),(0,0));
        let until=Instant::now()+LIMIT;let mut sequence=0;
        while sequence<5000 {
            assert!(Instant::now()<until,"actual burst did not drain");
            for row in runtime.recv(Duration::from_millis(20)).unwrap() {
                let action=&row["action"];if action["type"]!="raw_captured"{continue}
                assert_eq!(action["payload"]["u"],sequence);assert_eq!(action["membership"],1);assert_eq!(action["serial"],0);
                let captures=pool.captures.lock().unwrap();let (captured_sequence,captured)=captures[sequence as usize];
                assert_eq!(captured_sequence,sequence);assert_eq!(action["ingress_clock"],json!({"wall_ms":captured.wall_ms,"monotonic_ms":captured.monotonic_ms}));
                assert_eq!(action["payload"]["run_time_ms"],captured.wall_ms);
                let mut expected=payload(sequence);expected["run_time_ms"]=json!(captured.wall_ms);
                let route=action["route"].as_str().unwrap();let worker=&runtime.workers[route];
                expected["_ptk_connection_id"]=json!(worker.token);expected["_ptk_connection_generation"]=json!(worker.generation);
                assert_eq!(action["payload"],expected);assert_eq!(captured.wall_ms,WALL+captured.monotonic_ms);sequence+=1;
            }
        }
        fixture.sent.recv_timeout(LIMIT).unwrap();assert_eq!(pool.captures.lock().unwrap().len(),5000);
        assert_eq!((runtime.received.load(Ordering::Acquire),runtime.supplied,runtime.retired_frames),(5000,5000,0));assert_eq!(free(&pool),CAPACITY);
        runtime.request_handoff_shutdown().unwrap();let route=runtime.retiring.keys().next().unwrap().clone();await_finished(&runtime,&route);
        assert!(runtime.handoff_shutdown_ready().unwrap());runtime.shutdown().unwrap();fixture.finish().unwrap();
        assert_eq!((runtime.workers.len(),runtime.retiring.len(),free(&pool)),(0,0,CAPACITY));assert!(runtime.failure.lock().unwrap().is_none());
    }

    #[test]
    fn actual_held_post_ack_stop_retires_once_at_join_and_fatal_never_retires_it() {
        for fatal in [false,true] {
            let mut fixture=Fixture::start(CAPACITY as u64+1,false);let mut runtime=start(&fixture);await_ack(&mut runtime);
            let pool=runtime.ingress_credits.clone().unwrap();let memberships=runtime.memberships.clone();
            fixture.go.send(()).unwrap();await_pressure(&pool);fixture.sent.recv_timeout(LIMIT).unwrap();
            let route=runtime.workers.keys().next().unwrap().clone();assert_eq!((runtime.supplied,runtime.retired_frames),(0,0));
            if fatal {
                fail(&runtime.failure,"original waiting-worker failure".into());runtime.retire(&route).unwrap();assert_eq!(runtime.retired_frames,0);
                assert!(runtime.shutdown().is_err());assert_eq!(runtime.retired_frames,CAPACITY as u64);
                assert_eq!(runtime.failure.lock().unwrap().as_deref(),Some("original waiting-worker failure"));
            }else {
                runtime.detach_handoff(vec![route.clone()]).unwrap();assert_eq!(runtime.retired_frames,0);
                await_finished(&runtime,&route);runtime.reap_ready().unwrap();assert_eq!(runtime.retired_frames,1);
                for _ in 0..3 {runtime.reap_ready().unwrap();assert_eq!(runtime.retired_frames,1);}
                assert!(!runtime.generations.contains_key(&route)&&!runtime.serials.contains_key(&route));assert!(!runtime.control.physical.lock().unwrap().contains_key(&route));
                drain_retired(&mut runtime,CAPACITY);assert_eq!(runtime.retired_frames,CAPACITY as u64+1);assert_eq!(runtime.supplied,0);
                runtime.shutdown().unwrap();assert!(runtime.failure.lock().unwrap().is_none());
            }
            assert_eq!(runtime.memberships,memberships);assert_eq!(runtime.received.load(Ordering::Acquire),CAPACITY as u64+1);
            assert_eq!((runtime.workers.len(),runtime.retiring.len(),free(&pool)),(0,0,CAPACITY));fixture.finish().unwrap();
        }
    }

    #[test]
    fn actual_pre_ack_credit_cancellation_retains_unqualified_failure_and_zero_summary() {
        let mut fixture=Fixture::start(1,true);let mut runtime=Runtime::start_with_ingress_mode(fixture.settings(),LifecyclePolicy::default(),true,IngressMode::V2).unwrap();let pool=runtime.ingress_credits.clone().unwrap();
        // The controlled reservations delay Connected; pre-ACK buffering itself
        // consumes no credit and has already captured the sole real raw frame.
        let stop=AtomicBool::new(false);let held:Vec<_>=(0..CAPACITY).map(|_|pool.reserve(&stop,&runtime.failure,None).ok().unwrap()).collect();
        fixture.ack.send(()).unwrap();await_pressure(&pool);assert_eq!(runtime.received.load(Ordering::Acquire),1);assert_eq!(pool.captures.lock().unwrap().len(),1);
        let route=runtime.workers.keys().next().unwrap().clone();runtime.detach_handoff(vec![route.clone()]).unwrap();await_finished(&runtime,&route);
        runtime.reap_ready().unwrap();assert_eq!((runtime.supplied,runtime.retired_frames),(0,0));
        assert_eq!(runtime.failure.lock().unwrap().as_deref(),Some("retired 1 pre-ACK frames; owner incomplete"));
        assert!(runtime.handoff_shutdown_ready().is_err());drop(held);assert_eq!(free(&pool),CAPACITY);fixture.finish().unwrap();
    }

    #[test]
    fn actual_v2_environment_public_constructors_preserve_ack_flush_cancellation() {
        if std::env::var_os("CEA_NATIVE_INGRESS_COOPERATE").as_deref()!=Some(std::ffi::OsStr::new("v2")) {
            const METHOD:&str="runtime::ingress_credit_v2_tests::actual_v2_environment_public_constructors_preserve_ack_flush_cancellation";
            const OUTPUT_LIMIT:usize=64*1024;
            type Captured=(Vec<u8>,Option<String>);
            struct OwnedChild {child:std::process::Child,stdout:Option<JoinHandle<Captured>>,stderr:Option<JoinHandle<Captured>>}
            impl OwnedChild {
                fn capture<R:Read+Send+'static>(stream:R,overflow:Arc<AtomicBool>)->JoinHandle<Captured> {
                    thread::spawn(move||{
                        let mut bytes=Vec::new();
                        let error=stream.take((OUTPUT_LIMIT+1) as u64).read_to_end(&mut bytes).err().map(|error|error.to_string());
                        if bytes.len()>OUTPUT_LIMIT {overflow.store(true,Ordering::Release);}
                        (bytes,error)
                    })
                }
                fn joined(handle:Option<JoinHandle<Captured>>)->Captured {
                    match handle {
                        Some(join)=>join.join().unwrap_or_else(|_|(Vec::new(),Some("child capture thread panicked".into()))),
                        None=>(Vec::new(),Some("child capture handle missing".into())),
                    }
                }
            }
            impl Drop for OwnedChild {
                fn drop(&mut self) {
                    if !matches!(self.child.try_wait(),Ok(Some(_))){let _=self.child.kill();}
                    let _=self.child.wait();
                    if self.stdout.is_some(){let _=Self::joined(self.stdout.take());}
                    if self.stderr.is_some(){let _=Self::joined(self.stderr.take());}
                }
            }
            let deadline=Instant::now()+Duration::from_secs(20);
            let child=std::process::Command::new(std::env::current_exe().expect("current actual test executable"))
                .args(["--exact",METHOD,"--nocapture","--test-threads=1","--color=never"])
                .env("CEA_NATIVE_INGRESS_COOPERATE","v2")
                .stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped()).spawn().expect("spawn owned exact-method v2 child");
            let mut owned=OwnedChild{child,stdout:None,stderr:None};
            let overflow=Arc::new(AtomicBool::new(false));
            let stdout=owned.child.stdout.take().expect("owned child stdout pipe");
            owned.stdout=Some(OwnedChild::capture(stdout,overflow.clone()));
            let stderr=owned.child.stderr.take().expect("owned child stderr pipe");
            owned.stderr=Some(OwnedChild::capture(stderr,overflow.clone()));
            let outcome:std::result::Result<std::process::ExitStatus,String>=loop {
                if overflow.load(Ordering::Acquire){break Err("owned child output exceeded finite limit".into())}
                if Instant::now()>=deadline{break Err("owned exact-method child exceeded 20s deadline".into())}
                match owned.child.try_wait(){Ok(Some(status))=>break Ok(status),Ok(None)=>{},Err(error)=>break Err(error.to_string())}
                thread::sleep(deadline.saturating_duration_since(Instant::now()).min(Duration::from_millis(10)));
            };
            if outcome.is_err(){let _=owned.child.kill();}
            let reaped=owned.child.wait();
            let (stdout,stdout_error)=OwnedChild::joined(owned.stdout.take());
            let (stderr,stderr_error)=OwnedChild::joined(owned.stderr.take());
            eprintln!("ingress_credit_v2_child={}",json!({"method":METHOD,"pid":owned.child.id(),"wrapper_error":outcome.as_ref().err(),
                "exit_code":reaped.as_ref().ok().and_then(|status|status.code()),"wait_error":reaped.as_ref().err().map(|error|error.to_string()),
                "output_limit_bytes":OUTPUT_LIMIT,"stdout":String::from_utf8_lossy(&stdout),"stderr":String::from_utf8_lossy(&stderr),
                "stdout_bytes":stdout.len(),"stderr_bytes":stderr.len(),"stdout_error":stdout_error.as_deref(),"stderr_error":stderr_error.as_deref()}));
            assert!(outcome.is_ok(),"actual owned child failed bounded wait: {outcome:?}");
            assert!(stdout_error.is_none()&&stderr_error.is_none(),"actual child capture failed");
            assert!(stdout.len()<=OUTPUT_LIMIT&&stderr.len()<=OUTPUT_LIMIT,"actual child output exceeded finite limit");
            assert!(reaped.expect("reap owned exact-method child").success(),"actual exact-method child failed");
            let stdout=std::str::from_utf8(&stdout).expect("actual child libtest stdout must be UTF-8");
            let passed=format!("test {METHOD} ... ok");
            assert_eq!(stdout.lines().filter(|line|*line=="running 1 test").count(),1);
            assert!(stdout.lines().any(|line|line==passed.as_str()),"actual exact method did not pass in child");
            assert_eq!(stdout.lines().filter(|line|line.starts_with("test result: ")).count(),1);
            assert!(stdout.lines().any(|line|line.starts_with("test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; ")),"actual child did not report one genuine pass");
            return
        }
        assert_eq!(std::env::var_os("CEA_NATIVE_INGRESS_COOPERATE").as_deref(),Some(std::ffi::OsStr::new("v2")));
        let mut fixture=Fixture::start(1,true);
        let rejected=Runtime::start(fixture.settings()).err();
        assert_eq!(rejected.as_deref(),Some("native ingress v2 requires configured whole-plan runtime"));
        let mut runtime=Runtime::start_configured(fixture.settings()).unwrap();
        assert_eq!(runtime.ingress_mode,IngressMode::V2);assert!(runtime.handoff_enabled());assert_eq!(runtime.workers.len(),1);
        let pool=runtime.ingress_credits.clone().expect("public configured constructor must select v2 credits");
        let stop=AtomicBool::new(false);
        let held:Vec<_>=(0..CAPACITY-1).map(|_|pool.reserve(&stop,&runtime.failure,None).ok().unwrap()).collect();
        assert_eq!(held.len(),CAPACITY-1);assert_eq!(free(&pool),1);
        // The real ACK follows its pre-ACK Raw only after these reservations.
        // Connected consumes the last credit and remains queued through join.
        fixture.ack.send(()).unwrap();await_pressure(&pool);
        let captured={let captures=pool.captures.lock().unwrap();assert_eq!(captures.len(),1);captures[0]};
        assert_eq!(captured.0,0);assert!(captured.1.monotonic_ms>=0);assert_eq!(captured.1.wall_ms,WALL+captured.1.monotonic_ms);
        assert_eq!((runtime.received.load(Ordering::Acquire),runtime.supplied,runtime.retired_frames),(1,0,0));
        assert!(runtime.failure.lock().unwrap().is_none());
        let route=runtime.workers.keys().next().unwrap().clone();
        let physical=runtime.control.physical.lock().unwrap()[&route].clone();
        assert_eq!(physical["connected"],true);assert_eq!(physical["applied"],json!(["ETHUSDT"]));
        runtime.detach_handoff(vec![route.clone()]).unwrap();await_finished(&runtime,&route);runtime.reap_ready().unwrap();
        assert_eq!((runtime.workers.len(),runtime.retiring.len()),(0,0));assert!(runtime.control.physical.lock().unwrap().is_empty());
        assert_eq!((runtime.received.load(Ordering::Acquire),runtime.supplied,runtime.retired_frames),(1,0,0));
        assert_eq!(runtime.failure.lock().unwrap().as_deref(),Some("retired 1 pre-ACK frames; owner incomplete"));
        assert_eq!(runtime.handoff_shutdown_ready().unwrap_err(),"retired 1 pre-ACK frames; owner incomplete");
        {let state=pool.state.lock().unwrap();assert_eq!((state.free,state.waiting),(0,0));}
        // Only the worker's actual Connected can own the final credit here;
        // no dequeue occurred while the buffered Raw was waiting or stopping.
        let queued=runtime.receiver.try_recv().ok().expect("actual Connected must remain queued after stopped ACK flush");
        match &queued {
            Event::CreditedConnected{route:queued_route,token,generation,..}=>{
                assert_eq!(queued_route,&route);assert_eq!(json!(token),physical["token"]);assert_eq!(json!(generation),physical["generation"]);
            },
            _=>panic!("ACK flush must leave only actual credited Connected, never enqueue Raw"),
        }
        drop(queued.release_credit());assert_eq!(free(&pool),1);
        assert!(matches!(runtime.receiver.try_recv(),Err(mpsc::TryRecvError::Empty)));
        {let captures=pool.captures.lock().unwrap();assert_eq!(captures.len(),1);assert_eq!((captures[0].0,captures[0].1.wall_ms,captures[0].1.monotonic_ms),(captured.0,captured.1.wall_ms,captured.1.monotonic_ms));}
        drop(held);assert_eq!(free(&pool),CAPACITY);
        fixture.finish().unwrap();assert!(fixture.join.is_none());
        assert_eq!((runtime.received.load(Ordering::Acquire),runtime.supplied,runtime.retired_frames),(1,0,0));
        assert_eq!(runtime.failure.lock().unwrap().as_deref(),Some("retired 1 pre-ACK frames; owner incomplete"));
        drop(runtime);assert_eq!(free(&pool),CAPACITY);
    }
}
