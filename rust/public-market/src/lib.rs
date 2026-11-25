//! Bounded public Binance BBO parsing and explicit-clock selector.
//! No execution, account credentials, or automatic fallback belongs here.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::ops::Deref;
use std::sync::Arc;
pub mod transport;
pub mod runtime;
pub mod json;
mod decimal;
pub type Result<T> = std::result::Result<T, String>;
pub const CAPACITY: usize = 4096;
pub const MAX_SYMBOLS: usize = 64;
pub const MAX_LINE: usize = 65536;
pub const MAX_ACTIONS: usize = 1_048_576;
pub const MAX_BYTES: usize = 536_870_912;
const AGE: i64 = 1000;
const CONNECTION: &str = "_ptk_connection_id";
const GENERATION: &str = "_ptk_connection_generation";
const RECEIVE: &str = "run_time_ms";


/// Accepted quote data is immutable. Ordinary Clone deliberately retains the
/// original eager Value semantics, including derived Route::clone and oracles.
/// Only explicit cache-copy sites opt into sharing; output frames stay owned.
#[derive(Debug, PartialEq)]
struct ImmutableQuote { value: Arc<Value> }
impl ImmutableQuote {
    fn new(value: Value) -> Self { Self { value: Arc::new(value) } }
    fn owned(&self) -> Value {
        #[cfg(test)] immutable_quote_tests::record_owned();
        self.value.as_ref().clone()
    }
    fn share(&self) -> Self {
        #[cfg(test)]
        if immutable_quote_tests::eager_enabled() { return self.clone(); }
        #[cfg(test)] immutable_quote_tests::record_share();
        Self { value: Arc::clone(&self.value) }
    }
}
impl Clone for ImmutableQuote {
    fn clone(&self) -> Self { Self::new(self.owned()) }
}
impl Deref for ImmutableQuote {
    type Target = Value;
    fn deref(&self) -> &Value { self.value.as_ref() }
}
impl Serialize for ImmutableQuote {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        self.value.as_ref().serialize(serializer)
    }
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Clock { pub wall_ms: i64, pub monotonic_ms: i64 }
impl Clock {
    fn valid(self) -> bool {
        self.wall_ms > 0
            && self.wall_ms <= 4_000_000_000_000
            && (0..=i64::MAX - AGE).contains(&self.monotonic_ms)
    }
}
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Route {
    pub route: String, pub slot: u8, pub token: String, pub generation: u64,
    pub connected: bool, pub replacement: bool,
    #[serde(default)] pub applied: BTreeSet<String>,
    #[serde(skip)] serial: u64,
    #[serde(skip)] synced_serial: u64,
    #[serde(skip)] quotes: BTreeMap<String, ImmutableQuote>,
    #[serde(skip)] high: BTreeMap<String, ImmutableQuote>,
    #[serde(skip)] pending: VecDeque<(Value,u64,u64,Clock)>,
}
/// Physical control metadata only; no quote, pending input or watermark copies.
#[derive(Clone,Debug,PartialEq,Eq)]
pub struct RouteControl {
    pub route:String,pub slot:u8,pub token:String,pub generation:u64,
    pub connected:bool,pub replacement:bool,pub applied:BTreeSet<String>,
}
#[derive(Clone,Debug,PartialEq)]
pub struct CompactOutcome {pub action:&'static str,pub accepted:Option<bool>,pub frames:Vec<Value>}
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub struct SymbolControl {pub epoch:u64,pub available:bool,pub delivery_pending:bool}
/// Immutable reset qualification; delivery/pending state is intentionally absent.
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
pub struct ResetControl {pub epoch:u64,pub available:bool}
impl From<Route> for RouteControl {
    fn from(route:Route)->Self {Self{route:route.route,slot:route.slot,token:route.token,generation:route.generation,connected:route.connected,replacement:route.replacement,applied:route.applied}}
}
impl RouteControl {
    fn into_route(self)->Route {Route{route:self.route,slot:self.slot,token:self.token,generation:self.generation,connected:self.connected,replacement:self.replacement,applied:self.applied,serial:0,synced_serial:0,quotes:BTreeMap::new(),high:BTreeMap::new(),pending:VecDeque::new()}}
}
#[derive(Deserialize)]
#[serde(tag="type", deny_unknown_fields)]
pub enum Action {
    #[serde(rename="configure")]
    Configure { stream_id: String, mode: String, symbols: Vec<String>, #[serde(flatten)] clock: Clock },
    #[serde(rename="route")] Route { #[serde(flatten)] state: Route },
    #[serde(rename="targets")] Targets { symbols: Vec<String> },
    #[serde(rename="raw")] Raw { route: String, payload: Value, membership: u64, serial: u64, #[serde(flatten)] clock: Clock },
    #[serde(rename="raw_captured")] RawCaptured { route:String,payload:Value,membership:u64,serial:u64,ingress_clock:Clock,#[serde(flatten)] clock:Clock },
    #[serde(rename="poll")] Poll { output_capacity: usize, #[serde(flatten)] clock: Clock },
    #[serde(rename="gate")] Gate { frame: Value, #[serde(flatten)] clock: Clock },
    #[serde(rename="gap")] Gap { route: String },
    #[serde(rename="retire")] Retire { route: String },
    #[serde(rename="snapshot")] Snapshot { #[serde(flatten)] clock: Clock },
    #[serde(rename="close")] Close,
}
fn integer(v: &Value, key: &str) -> Option<i64> { v.get(key).and_then(Value::as_i64) }
fn update(v: &Value) -> i64 { integer(v,"u").unwrap_or(-1) }
fn source(v: &Value) -> Option<i64> { match v.get("E") {Some(e) if !e.is_null()=>e.as_i64(),_=>integer(v,"T")} }
fn text<'a>(v: &'a Value, key: &str) -> Option<&'a str> { v.get(key).and_then(Value::as_str) }
fn symbol_valid(s: &str) -> bool { !s.is_empty() && s.len() <= 32 && s.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_') }
fn fingerprint(a: &Value, b: &Value) -> bool { ["u","b","a","B","A","E","T"].iter().all(|k| a.get(*k)==b.get(*k)) }
fn physical(a: &Value, b: &Value) -> bool { [CONNECTION,GENERATION].iter().all(|k| a.get(*k)==b.get(*k)) }
/// Configured native admission only; shared parsing retains its legacy domain.
pub fn reject_numeric_bookticker_fields(payload:&Value)->Result<()> {
    if ["b","a","B","A"].iter().any(|key|payload.get(*key).is_some_and(Value::is_number)) {
        return Err("native BN bookTicker requires decimal-string prices and sizes".into())
    }
    Ok(())
}
fn decimal_text(value: &Value) -> Option<String> {
    value.as_str().map(str::to_owned).or_else(|| value.as_number().map(ToString::to_string))
}
fn prices_reference(v: &Value) -> bool {
    let values: Option<Vec<_>> = ["b", "a", "B", "A"]
        .iter()
        .map(|key| {
            let text = decimal_text(v.get(*key)?)?;
            decimal::PositiveDecimal::parse(&text)
        })
        .collect();
    values.is_some_and(|values| values[0].compare(&values[1]).is_le())
}
/// Borrowed subset of PositiveDecimal: digits+ with an optional .digits+.
/// Every byte is checked before comparison; no coefficient/exponent is built.
struct PlainDecimal<'a> {
    integer: &'a [u8],
    fraction: &'a [u8],
    positive: bool,
}
impl<'a> PlainDecimal<'a> {
    fn parse(text: &'a str) -> Option<Self> {
        let bytes = text.as_bytes();
        if !bytes.first()?.is_ascii_digit() || !bytes.last()?.is_ascii_digit() {
            return None;
        }
        let mut point = None;
        let mut positive = false;
        for (index, byte) in bytes.iter().copied().enumerate() {
            match byte {
                b'0'..=b'9' => positive |= byte != b'0',
                b'.' if point.is_none() => point = Some(index),
                _ => return None,
            }
        }
        let (integer, fraction) = match point {
            Some(index) => (&bytes[..index], &bytes[index + 1..]),
            None => (bytes, &bytes[bytes.len()..]),
        };
        let first = integer.iter().position(|byte| *byte != b'0').unwrap_or(integer.len());
        Some(Self { integer: &integer[first..], fraction, positive })
    }
    fn compare(&self, other: &Self) -> std::cmp::Ordering {
        self.integer.len().cmp(&other.integer.len())
            .then_with(|| self.integer.cmp(other.integer))
            .then_with(|| {
                (0..self.fraction.len().max(other.fraction.len()))
                    .map(|index| self.fraction.get(index).copied().unwrap_or(b'0')
                        .cmp(&other.fraction.get(index).copied().unwrap_or(b'0')))
                    .find(|order| !order.is_eq())
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
    }
}
fn plain_prices(v: &Value) -> Option<bool> {
    let bid = PlainDecimal::parse(text(v, "b")?)?;
    let ask = PlainDecimal::parse(text(v, "a")?)?;
    let bid_size = PlainDecimal::parse(text(v, "B")?)?;
    let ask_size = PlainDecimal::parse(text(v, "A")?)?;
    Some(bid.positive && ask.positive && bid_size.positive && ask_size.positive
        && bid.compare(&ask).is_le())
}
fn prices(v: &Value) -> bool {
    #[cfg(test)]
    if plain_decimal_tests::reference_enabled() { return prices_reference(v); }
    plain_prices(v).unwrap_or_else(|| prices_reference(v))
}
fn age_within(now: i64, stamp: i64) -> bool {
    now.checked_sub(stamp)
        .is_some_and(|age| (0..=AGE).contains(&age))
}
fn fresh(v: &Value, clock: Clock) -> bool {
    [(integer(v,"_ptk_ingress_monotonic_ms"),clock.monotonic_ms),
     (integer(v,RECEIVE),clock.wall_ms),(source(v),clock.wall_ms)].iter()
     .all(|(stamp,now)|stamp.is_some_and(|stamp| age_within(*now,stamp)))
}
fn coverage_expiry(v: &Value, clock: Clock) -> Option<i64> {
    let ingress = integer(v, "_ptk_ingress_monotonic_ms")?;
    let source_age = clock.wall_ms.checked_sub(source(v)?)?;
    let receive_age = clock.wall_ms.checked_sub(integer(v, RECEIVE)?)?;
    ingress
        .checked_add(AGE)?
        .checked_sub(source_age.max(receive_age).max(0))
}
#[derive(Default)]
pub struct Selector {
    stream: String, mode: String, stopped: bool, started: bool, first_failure: Option<String>,
    targets: BTreeSet<String>, members: BTreeMap<String,u64>, epochs: BTreeMap<String,u64>,
    routes: Vec<Route>, available: BTreeMap<String,bool>, selected: BTreeMap<String,String>,
    observed: BTreeMap<String,ImmutableQuote>, primary: BTreeMap<String,ImmutableQuote>, staged: BTreeMap<String,ImmutableQuote>,
    enqueued: BTreeMap<String,ImmutableQuote>, gated: BTreeMap<String,i64>, outbox: VecDeque<Value>,
    gap_fenced: BTreeSet<String>, coverage: BTreeMap<String,i64>,
    rejections: BTreeMap<String,u64>, delivery_drops: u64, ingress_drops: u64,
    clock: Option<Clock>,
    sweep_due_ms: i64,
    sweep_states: Option<Value>,
    #[cfg(test)] reference_eager_poll:bool,
    #[cfg(test)] route_state_constructions:std::cell::Cell<usize>,
    #[cfg(test)] poll_sweeps:Vec<i64>,
    #[cfg(test)] selection_calls:Vec<(i64,i64,bool,Option<String>)>,
    #[cfg(test)] reference_deep_clone:bool,
    #[cfg(test)] reference_clone_count:usize,
    #[cfg(test)] reference_eager_select:bool,
    // Explicit selected-quote clone sites only, not allocator or nested Route copies.
    #[cfg(test)] selection_quote_clones:usize,
}
impl Selector {
    /// Same target membership, epoch and availability as `symbol_control`.
    /// Does not qualify quotes, advance clocks or inspect delivery watermarks.
    pub fn reset_control(&self,symbol:&str)->Option<ResetControl> {
        self.targets.contains(symbol).then(||ResetControl{epoch:self.epochs[symbol],available:self.available.get(symbol)==Some(&true)})
    }
    pub fn symbol_control(&self,symbol:&str)->Option<SymbolControl> {
        self.targets.contains(symbol).then(||SymbolControl{epoch:self.epochs[symbol],available:self.available.get(symbol)==Some(&true),delivery_pending:self.staged.get(symbol).is_some_and(|h|update(h)>*self.gated.get(symbol).unwrap_or(&-1))})
    }
    /// Identical transitions and failure latch, with no public-state projection.
    pub fn apply_compact(&mut self,action:Action)->Result<CompactOutcome> {
        if let Some(failure)=&self.first_failure{return Err(failure.clone())}
        let result=self.apply_core(action);self.retain_failure(result)
    }
    pub fn route_ids(&self)->impl Iterator<Item=&str> {self.routes.iter().map(|r|r.route.as_str())}
    /// Same Route action validation/transitions, without diagnostic projection.
    pub fn apply_route_control(&mut self,control:RouteControl)->Result<()> {
        if let Some(failure)=&self.first_failure{return Err(failure.clone())}
        let result=if !self.started||self.stopped {Err("selector unconfigured or stopped".into())}else{self.update_route(control.into_route())};
        self.retain_failure(result)
    }
    /// Reapply decoded physical truth at every synchronization boundary.
    /// An unchanged descriptor may still restore a separately changed route.
    pub fn apply_route_control_ref(&mut self,control:&RouteControl)->Result<()> {
        if let Some(failure)=&self.first_failure{return Err(failure.clone())}
        let result=if !self.started||self.stopped {Err("selector unconfigured or stopped".into())}else{
            self.update_route_control_ref(control)
        };
        self.retain_failure(result)
    }
    fn update_route_control_ref(&mut self,state:&RouteControl)->Result<()> {
        self.validate_route_fields(state.slot,&state.route,&state.token,state.generation)?;
        if let Some(r)=self.routes.iter_mut().find(|r|r.route==state.route) {
            // Existing slots remain immutable, matching the owned Route API.
            if r.token!=state.token {r.token.clone_from(&state.token);}
            r.generation=state.generation;r.connected=state.connected;r.replacement=state.replacement;
            if r.applied!=state.applied {r.applied.clone_from(&state.applied);}
        }else{
            if self.routes.len()>=3{return Err("route capacity exceeded".into())}
            self.routes.push(state.clone().into_route());
        }
        Ok(())
    }
    pub fn retire_route_control(&mut self,route:&str)->Result<()> {
        if let Some(failure)=&self.first_failure{return Err(failure.clone())}
        let result=if !self.started||self.stopped {Err("selector unconfigured or stopped".into())}else{self.retire_route(route)};
        self.retain_failure(result)
    }
    fn validate_route_identity(&self,state:&Route)->Result<()> {
        self.validate_route_fields(state.slot,&state.route,&state.token,state.generation)
    }
    fn validate_route_fields(&self,slot:u8,route:&str,token:&str,generation:u64)->Result<()> {
        if slot>1||self.mode=="single"&&slot!=0||route.is_empty()||route.len()>64||token.is_empty()||token.len()>64||generation==0 {return Err("invalid route identity".into())}
        Ok(())
    }
    fn update_route(&mut self,state:Route)->Result<()> {
        self.validate_route_identity(&state)?;
        if let Some(r)=self.routes.iter_mut().find(|r|r.route==state.route) {
            // Slot validation is repeated, but an existing route's slot remains
            // immutable exactly as in the original Route action.
            if r.token!=state.token||r.generation!=state.generation||r.connected!=state.connected||r.applied!=state.applied||r.replacement!=state.replacement {
                r.token=state.token;r.generation=state.generation;r.connected=state.connected;r.applied=state.applied;r.replacement=state.replacement;
            }
        }else{if self.routes.len()>=3{return Err("route capacity exceeded".into())}self.routes.push(state);}
        Ok(())
    }
    fn retire_route(&mut self,route:&str)->Result<()> {
        let i=self.routes.iter().position(|r|r.route==route).ok_or("unknown route")?;self.routes.remove(i);Ok(())
    }
    pub fn replacement_ready(&mut self,route:&str,clock:Clock)->Result<bool>{
        self.set_clock(clock)?;self.qualify()?;
        Ok(self.routes.iter().find(|r|r.route==route).is_some_and(|r|r.replacement&&r.connected&&r.applied==self.targets&&self.targets.iter().all(|s|self.qualified(r,s,clock,self.mode=="shadow"&&r.slot==0))))
    }
    fn reject(&mut self, reason: &str) { *self.rejections.entry(reason.into()).or_default()+=1; }
    fn epoch(&mut self,s: &str) { *self.epochs.entry(s.into()).or_default()+=1; }
    fn current_route(&self, route: &Route, v: &Value) -> bool {
        route.connected && text(v,CONNECTION)==Some(route.token.as_str()) && v.get(GENERATION).and_then(Value::as_u64)==Some(route.generation)
    }
    fn contributes(&self, route: &Route) -> bool { self.mode=="redundant" || route.slot==0 && !route.replacement }
    fn qualified(&self, route: &Route, s: &str, clock: Clock, primary: bool) -> bool {
        let Some(v)=route.quotes.get(s) else { return false };
        let high=if primary { &self.primary } else { &self.observed };
        self.current_route(route,v) && v["_ptk_membership_version"].as_u64()==self.members.get(s).copied() && fresh(v,clock) && high.get(s).is_none_or(|h|update(v)>=update(h) && source(v)>=source(h) && (update(v)!=update(h)||fingerprint(v,h)))
    }
    fn unavailable(&mut self,s: &str) {
        if self.available.get(s)==Some(&true) && !self.gap_fenced.contains(s) { self.epoch(s); }
        self.available.insert(s.into(),false); self.gap_fenced.remove(s);
    }
    #[cfg(test)]
    fn selection_snapshot(&mut self,index:usize,symbol:&str)->(String,u64,Value) {
        self.selection_quote_clones+=1;
        if self.reference_deep_clone {
            self.reference_clone_count+=1;
            let route=self.routes[index].clone();let quote=route.quotes[symbol].owned();
            return (route.route,route.serial,quote)
        }
        let route=&self.routes[index];
        (route.route.clone(),route.serial,route.quotes[symbol].owned())
    }
    fn select(&mut self, clock: Clock, publish: bool, scope: Option<&str>) {
        #[cfg(test)]
        if self.reference_eager_select || self.reference_deep_clone {
            self.select_eager_reference(clock,publish,scope);
            return;
        }
        #[cfg(test)] self.selection_calls.push((clock.wall_ms,clock.monotonic_ms,publish,scope.map(str::to_owned)));
        if self.stopped { return }
        let symbols: Vec<_>=self.targets.iter().filter(|s|scope.is_none_or(|scope|scope==s.as_str())).cloned().collect();
        for s in symbols {
            let candidates: Vec<_>=self.routes.iter().enumerate().filter(|(_,r)|self.contributes(r) && self.qualified(r,&s,clock,self.mode=="shadow")).map(|(i,_)|i).collect();
            let preferred=candidates.iter().find(|i|self.selected.get(&s)==Some(&self.routes[**i].route)).copied();
            let chosen=preferred.or_else(||candidates.iter().rev().copied().max_by_key(|i| update(&self.routes[*i].quotes[&s])));
            let Some(i)=chosen else { self.unavailable(&s); continue };
            let route=&self.routes[i];
            let quote=&route.quotes[&s];
            let Some(expiry) = coverage_expiry(quote, clock) else {
                self.unavailable(&s);
                self.reject("timestamp");
                continue;
            };
            let (route_id,serial,sequence)=(route.route.clone(),route.serial,update(quote));
            self.selected.insert(s.clone(),route_id.clone()); self.available.insert(s.clone(),true); self.gap_fenced.remove(&s);
            self.coverage.entry(s.clone()).and_modify(|v|*v=(*v).max(expiry)).or_insert(expiry);
            if !publish { continue }
            let high=self.staged.get(&s).or_else(||self.enqueued.get(&s));
            if high.is_some_and(|h|sequence<=update(h)) { continue }
            #[cfg(test)] {self.selection_quote_clones+=1;}
            self.staged.insert(s.clone(),self.routes[i].quotes[&s].share());
            if self.outbox.len()>=CAPACITY*3 { self.delivery_drops+=1; self.epoch(&s); self.reject("delivery_outbox_full"); continue }
            #[cfg(test)] {self.selection_quote_clones+=1;}
            let mut frame=self.routes[i].quotes[&s].owned();
            frame["_ptk_logical_feed_id"]=json!(self.stream); frame["_ptk_symbol_epoch"]=json!(self.epochs[&s]);
            frame["_ptk_physical_route"]=json!(route_id); frame["_ptk_ingress_serial"]=json!(serial);
            self.outbox.push_back(frame);
        }
    }
    #[cfg(test)]
    fn select_eager_reference(&mut self, clock: Clock, publish: bool, scope: Option<&str>) {
        // Frozen pre-borrow select and selection_snapshot behavior. Never delegate
        // to the optimized select, including on suppressed and no-publish paths.
        #[cfg(test)] self.selection_calls.push((clock.wall_ms,clock.monotonic_ms,publish,scope.map(str::to_owned)));
        if self.stopped { return }
        let symbols: Vec<_>=self.targets.iter().filter(|s|scope.is_none_or(|scope|scope==s.as_str())).cloned().collect();
        for s in symbols {
            let candidates: Vec<_>=self.routes.iter().enumerate().filter(|(_,r)|self.contributes(r) && self.qualified(r,&s,clock,self.mode=="shadow")).map(|(i,_)|i).collect();
            let preferred=candidates.iter().find(|i|self.selected.get(&s)==Some(&self.routes[**i].route)).copied();
            let chosen=preferred.or_else(||candidates.iter().rev().copied().max_by_key(|i| update(&self.routes[*i].quotes[&s])));
            let Some(i)=chosen else { self.unavailable(&s); continue };
            let (route_id,serial,quote)=self.selection_snapshot(i,&s);
            let Some(expiry) = coverage_expiry(&quote, clock) else {
                self.unavailable(&s);
                self.reject("timestamp");
                continue;
            };
            self.selected.insert(s.clone(),route_id.clone()); self.available.insert(s.clone(),true); self.gap_fenced.remove(&s);
            self.coverage.entry(s.clone()).and_modify(|v|*v=(*v).max(expiry)).or_insert(expiry);
            if !publish { continue }
            let high=self.staged.get(&s).or_else(||self.enqueued.get(&s));
            if high.is_some_and(|h|update(&quote)<=update(h)) { continue }
            self.selection_quote_clones+=1;
            self.staged.insert(s.clone(),ImmutableQuote::new(quote.clone()));
            if self.outbox.len()>=CAPACITY*3 { self.delivery_drops+=1; self.epoch(&s); self.reject("delivery_outbox_full"); continue }
            let mut frame=quote;
            frame["_ptk_logical_feed_id"]=json!(self.stream); frame["_ptk_symbol_epoch"]=json!(self.epochs[&s]);
            frame["_ptk_physical_route"]=json!(route_id); frame["_ptk_ingress_serial"]=json!(serial);
            self.outbox.push_back(frame);
        }
    }
    fn reason(&self,route: &Route,v: &Value,clock: Clock) -> Option<&'static str> {
        let s=text(v,"s").unwrap_or("");
        if !self.targets.contains(s) { return Some("removed_symbol") }
        if v["_ptk_membership_version"].as_u64()!=self.members.get(s).copied() { return Some("retired_subscription") }
        if !self.current_route(route,v) { return Some("retired_generation") }
        if !prices(v)||update(v)<0 { return Some("malformed") }
        if !fresh(v,clock) { return Some("timestamp") }
        let previous=route.high.get(s);
        if let Some(p)=previous {
            if physical(p,v)&&update(v)<=update(p) { return Some("route_duplicate_or_regression") }
            if source(v)<source(p) { return Some("source_regression") }
        }
        let high=if self.mode=="shadow" && route.slot==0 {self.primary.get(s)}else{self.observed.get(s)};
        if high.is_some_and(|h|update(v)<update(h))||previous.is_some_and(|h|update(v)<update(h)) { return Some("global_regression") }
        if high.is_some_and(|h|update(v)==update(h)&&!fingerprint(v,h)) { return Some("conflicting_duplicate") }
        if high.is_some_and(|h|source(v)<source(h)) { return Some("source_regression") }
        None
    }
    fn ingest(&mut self,i: usize,mut v: Value,membership: u64,serial: u64,clock: Clock) -> Result<()> {
        if !v.is_object() { self.reject("malformed"); return Ok(()) }
        if serial!=self.routes[i].serial { self.reject("ingress_retired"); return Ok(()) }
        v["_ptk_ingress_monotonic_ms"]=json!(clock.monotonic_ms); v["_ptk_ingress_wall_ms"]=json!(clock.wall_ms);
        v["_ptk_membership_version"]=json!(membership);
        let s=text(&v,"s").unwrap_or("").to_owned();
        if let Some(reason)=self.reason(&self.routes[i],&v,clock) {
            self.reject(reason);
            if reason=="retired_generation" && self.contributes(&self.routes[i]) && update(&v)>*self.gated.get(&s).unwrap_or(&-1) && self.targets.contains(&s) && self.members.get(&s)==Some(&membership) && prices(&v) && update(&v)>=0 && fresh(&v,clock) && !self.routes.iter().any(|r|self.contributes(r)&&self.qualified(r,&s,clock,false)&&fingerprint(&r.quotes[&s],&v)) {
                self.epoch(&s); self.reject("retired_raw_transition");
            }
            return Ok(())
        }
        let Some(expiry) = coverage_expiry(&v, clock) else {
            self.reject("timestamp");
            return Ok(());
        };
        let v=ImmutableQuote::new(v);
        self.routes[i].quotes.insert(s.clone(),v.share()); self.routes[i].high.insert(s.clone(),v.share());
        if self.observed.get(&s).is_none_or(|h|update(&v)>update(h)&&source(&v)>=source(h)) { self.observed.insert(s.clone(),v.share()); }
        if self.routes[i].slot==0 && !self.routes[i].replacement { self.primary.insert(s.clone(),v); }
        if self.contributes(&self.routes[i]) {
            if self.coverage.get(&s).is_some_and(|until|clock.monotonic_ms>*until) {self.unavailable(&s);}
            self.available.insert(s.clone(),true);
            self.coverage.entry(s.clone()).and_modify(|v|*v=(*v).max(expiry)).or_insert(expiry);
            let now=self.clock.unwrap_or(clock);
            if fresh(&self.routes[i].quotes[&s],now) {self.select(now,true,Some(&s));}
            else {let quote=self.routes[i].quotes[&s].share();if self.staged.get(&s).or_else(||self.enqueued.get(&s)).is_none_or(|h|update(&quote)>update(h)){self.epoch(&s);self.staged.insert(s.clone(),quote);self.reject("expired_transition");}}
        }
        Ok(())
    }
    fn delivery_current(&self,v: &Value,clock: Clock) -> bool {
        let s=text(v,"s").unwrap_or("");
        !self.stopped && self.targets.contains(s) && self.available.get(s)==Some(&true) && fresh(v,clock) && text(v,"_ptk_logical_feed_id")==Some(self.stream.as_str()) && v["_ptk_symbol_epoch"].as_u64()==self.epochs.get(s).copied() && v["_ptk_membership_version"].as_u64()==self.members.get(s).copied() && self.routes.iter().any(|r|Some(r.route.as_str())==text(v,"_ptk_physical_route") && v["_ptk_ingress_serial"].as_u64()==Some(r.serial) && self.current_route(r,v))
    }
    fn retire_transition(&mut self,v: &Value) -> bool {
        let s=text(v,"s").unwrap_or("");
        if self.targets.contains(s) && text(v,"_ptk_logical_feed_id")==Some(self.stream.as_str()) && v["_ptk_symbol_epoch"].as_u64()==self.epochs.get(s).copied() && update(v)>*self.gated.get(s).unwrap_or(&-1) { self.epoch(s); self.reject("undeliverable_transition"); return true }
        false
    }
    fn flush(&mut self,clock: Clock,capacity: usize) -> Vec<Value> {
        let mut frames=Vec::new();
        while let Some(v)=self.outbox.pop_front() {
            let s=text(&v,"s").unwrap().to_owned();
            if !self.delivery_current(&v,clock) { self.delivery_drops+=1; self.retire_transition(&v); continue }
            if self.enqueued.get(&s).is_some_and(|h|update(&v)<=update(h)) {continue}
            if frames.len()>=capacity {
                self.delivery_drops+=1; self.reject("output_full");
                for r in &mut self.routes {r.quotes.remove(&s);}
                self.unavailable(&s); continue
            }
            self.enqueued.insert(s,ImmutableQuote::new(v.clone())); frames.push(v);
        }
        frames
    }
    fn targets(&mut self,symbols: Vec<String>) -> Result<()> {
        let targets: BTreeSet<_>=symbols.iter().cloned().collect();
        if targets.len()>MAX_SYMBOLS || symbols.len()!=targets.len() || targets.iter().any(|s|!symbol_valid(s)) {return Err("invalid/duplicate or excess symbols".into())}
        let changed: Vec<_>=self.targets.symmetric_difference(&targets).cloned().collect();
        for s in changed { *self.members.entry(s.clone()).or_default()+=1;
            if !targets.contains(&s) {self.epoch(&s);self.available.remove(&s);self.selected.remove(&s);self.staged.remove(&s);self.gated.remove(&s);self.coverage.remove(&s);self.gap_fenced.remove(&s);for r in &mut self.routes {r.quotes.remove(&s);}}
            else {self.epochs.entry(s.clone()).or_default();self.available.insert(s,false);}
        }
        // Lifetime membership/highwater storage is bounded, including removed targets.
        if self.members.len()>MAX_SYMBOLS {return Err("lifetime symbol capacity exceeded".into())}
        if self.targets!=targets {self.sweep_due_ms=0;}
        self.targets=targets; Ok(())
    }
    fn state_row(&self,symbol:&str)->Value {
        json!({"epoch":self.epochs[symbol],"available":self.available.get(symbol).copied().unwrap_or(false),"delivery_pending":self.staged.get(symbol).is_some_and(|h|update(h)>*self.gated.get(symbol).unwrap_or(&-1))})
    }
    /// Immutable projection only; callers retain the whole-cohort Snapshot action.
    pub fn state_for_symbol(&self,symbol:&str)->Value {
        let mut symbols=BTreeMap::new();
        if self.targets.contains(symbol) {symbols.insert(symbol,self.state_row(symbol));}
        json!({"stream_id":self.stream,"generation":1,"connected":self.started&&!self.stopped,"logical":true,"symbols":symbols,"symbol_scope":symbol})
    }
    pub fn state(&self) -> Value {
        let symbols: BTreeMap<_,_>=self.targets.iter().map(|s|(s,self.state_row(s))).collect();
        json!({"stream_id":self.stream,"generation":1,"connected":self.started&&!self.stopped,"logical":true,"symbols":symbols})
    }
    pub fn diagnostics(&self) -> Value {
        json!({"first_failure":self.first_failure,"rejections":self.rejections,"delivery_drops":self.delivery_drops,"ingress_drops":self.ingress_drops,"delivery_outbox_depth":self.outbox.len(),"observed":self.observed,"primary":self.primary,"staged":self.staged,"enqueued":self.enqueued,"gated":self.gated,"memberships":self.members,"selected":self.selected,"routes":self.routes})
    }
    pub fn apply(&mut self,action: Action) -> Result<Value> { self.apply_with_diagnostics(action,true) }
    pub fn apply_with_diagnostics(&mut self,action: Action,diagnostics: bool) -> Result<Value> {
        if let Some(failure)=&self.first_failure {return Err(failure.clone())}
        let result=self.apply_inner(action,diagnostics);
        self.retain_failure(result)
    }
    fn retain_failure<T>(&mut self,result:Result<T>)->Result<T> {
        if let Err(failure)=&result {
            self.first_failure=Some(failure.clone());self.stopped=true;
            self.delivery_drops+=self.outbox.len() as u64;self.outbox.clear();
            for s in self.targets.clone(){self.unavailable(&s);}
        }
        result
    }
    fn apply_inner(&mut self,action: Action,diagnostics: bool) -> Result<Value> {
        let outcome=self.apply_core(action)?;
        Ok(json!({"type":"result","action":outcome.action,"accepted":outcome.accepted,"frames":outcome.frames,"state":self.state(),"diagnostics":if diagnostics {self.diagnostics()}else{Value::Null}}))
    }
    fn apply_core(&mut self,action:Action)->Result<CompactOutcome> {
        let mut frames=Vec::new();let mut accepted=None;
        let kind=match action {
            Action::Configure{stream_id,mode,symbols,clock}=>{
                if self.started || stream_id.is_empty()||stream_id.len()>64||!matches!(mode.as_str(),"single"|"shadow"|"redundant")||!clock.valid(){return Err("invalid configure".into())}
                self.stream=stream_id;self.mode=mode;self.targets(symbols)?;self.clock=Some(clock);self.started=true;"configure"
            },
            other=>{
                if !self.started||self.stopped {return Err("selector unconfigured or stopped".into())}
                match other {
                    Action::Route{state}=>{
                        self.validate_route_identity(&state)?;
                        // Preserve the original unconditional assignment path
                        // as the full-output API and compact-sync oracle.
                        if let Some(r)=self.routes.iter_mut().find(|r|r.route==state.route) {r.token=state.token;r.generation=state.generation;r.connected=state.connected;r.applied=state.applied;r.replacement=state.replacement;}
                        else {if self.routes.len()>=3{return Err("route capacity exceeded".into())} self.routes.push(state);}
                        "route"
                    },
                    Action::Targets{symbols}=>{self.targets(symbols)?;"targets"},
                    Action::Raw{route,payload,membership,serial,clock}=>{
                        self.set_clock(clock)?;
                        let i=self.routes.iter().position(|r|r.route==route).ok_or("unknown route")?;
                        if self.routes[i].pending.len()>=CAPACITY { self.routes[i].serial+=1; self.ingress_drops+=1; }
                        else {self.routes[i].pending.push_back((payload,membership,serial,clock));} "raw"
                    },
                    Action::RawCaptured{route,payload,membership,serial,clock,ingress_clock}=>{
                        self.set_clock(clock)?;
                        if !ingress_clock.valid()||ingress_clock.monotonic_ms>clock.monotonic_ms{return Err("invalid captured ingress clock".into())}
                        let i=self.routes.iter().position(|r|r.route==route).ok_or("unknown route")?;
                        if self.routes[i].pending.len()>=CAPACITY {return Err("captured native ingress full".into())}
                        self.routes[i].pending.push_back((payload,membership,serial,ingress_clock));"raw_captured"
                    },
                    Action::Poll{output_capacity,clock}=>{self.set_clock(clock)?;if output_capacity>CAPACITY{return Err("output capacity exceeds4096".into())} self.qualify()?;
                        #[cfg(test)]
                        if self.reference_eager_poll {
                            self.poll_eager_reference(clock);
                            return Ok(CompactOutcome{action:"poll",accepted,frames:self.flush(clock,output_capacity)});
                        }
                        if !self.route_states_match()||clock.monotonic_ms>=self.sweep_due_ms {
                            let next_sweep_due = clock
                                .monotonic_ms
                                .checked_add(20)
                                .ok_or("invalid sweep deadline")?;
                            let states=self.route_states();
                            #[cfg(test)] self.poll_sweeps.push(clock.monotonic_ms);
                            self.select(clock,true,None);self.sweep_states=Some(states);self.sweep_due_ms=next_sweep_due;
                        }
                        frames=self.flush(clock,output_capacity);"poll"},
                    Action::Snapshot{clock}=>{self.set_clock(clock)?;self.qualify()?;self.select(clock,false,None);"snapshot"},
                    Action::Gate{frame,clock}=>{self.set_clock(clock)?;let s=text(&frame,"s").ok_or("invalid gate symbol")?.to_owned();self.qualify()?;self.select(clock,false,Some(&s));let valid=self.delivery_current(&frame,clock);if valid {let gate=self.gated.entry(s).or_insert(-1);*gate=(*gate).max(update(&frame));}else if self.retire_transition(&frame){self.delivery_drops+=1;} accepted=Some(valid);"gate"},
                    Action::Gap{route}=>{let i=self.routes.iter().position(|r|r.route==route).ok_or("unknown route")?;self.routes[i].serial+=1;self.ingress_drops+=1;"gap"},
                    Action::Retire{route}=>{self.retire_route(&route)?;"retire"},
                    Action::Close=>{self.stopped=true;for s in self.targets.clone(){self.unavailable(&s);}self.delivery_drops+=self.outbox.len() as u64;self.outbox.clear();self.routes.clear();"close"},
                    Action::Configure{..}=>unreachable!(),
                }
            }
        };
        Ok(CompactOutcome{action:kind,accepted,frames})
    }
    // Compare exactly the JSON shape built by route_states, without allocating.
    fn route_states_match(&self)->bool {
        let Some(states)=self.sweep_states.as_ref().and_then(Value::as_object) else {return false};
        states.len()==self.routes.len() && self.routes.iter().all(|route| {
            let Some(state)=states.get(&route.route).and_then(Value::as_object) else {return false};
            state.len()==3 && state.get("stream_id").and_then(Value::as_str)==Some(route.token.as_str())
                && state.get("generation").and_then(Value::as_u64)==Some(route.generation)
                && state.get("connected").and_then(Value::as_bool)==Some(route.connected)
        })
    }
    #[cfg(test)]
    fn poll_eager_reference(&mut self,clock:Clock) {
        // Frozen pre-change Poll branch: the expected side always constructs.
        let states=self.route_states();
        if self.sweep_states.as_ref()!=Some(&states)||clock.monotonic_ms>=self.sweep_due_ms {
            let next_sweep_due = clock
                .monotonic_ms
                .checked_add(20)
                .expect("validated clock bound prevents sweep overflow");
            self.poll_sweeps.push(clock.monotonic_ms);
            self.select(clock,true,None);self.sweep_states=Some(states);self.sweep_due_ms=next_sweep_due;
        }
    }
    fn route_states(&self)->Value {
        #[cfg(test)] self.route_state_constructions.set(self.route_state_constructions.get()+1);
        let states:BTreeMap<_,_>=self.routes.iter().map(|route|(&route.route,json!({"stream_id":route.token,"generation":route.generation,"connected":route.connected}))).collect();
        json!(states)
    }
    fn qualify(&mut self)->Result<()> {
        let mut pending=Vec::new();
        for i in 0..self.routes.len() {
            if self.routes[i].serial!=self.routes[i].synced_serial {
                self.routes[i].synced_serial=self.routes[i].serial;
                self.routes[i].quotes.clear();self.reject("ingress_gap");
                if self.contributes(&self.routes[i]) {for s in self.targets.clone(){self.epoch(&s);self.gap_fenced.insert(s);}}
            }
        }
        for (i,r) in self.routes.iter_mut().enumerate() {
            while let Some((v,m,s,c))=r.pending.pop_front(){pending.push((i,v,m,s,c));}
        }
        pending.sort_by_key(|(_,v,_,_,c)|(c.monotonic_ms,update(v)));
        for (i,v,m,s,c) in pending {self.ingest(i,v,m,s,c)?;}
        Ok(())
    }
    fn set_clock(&mut self,clock: Clock)->Result<()> {
        if !clock.valid() || self.clock.is_some_and(|old|clock.monotonic_ms<old.monotonic_ms) {return Err("invalid or regressing clock".into())} self.clock=Some(clock);Ok(())
    }
}

/// Lossless BN top-of-book conversion. Spot without exchange timestamps uses
/// explicit local-receive provenance, exactly as the Python physical manager.
#[derive(Clone,Debug,Serialize)]
pub struct BookTicker {
    pub symbol:String,pub bid:String,pub ask:String,pub bid_size:String,pub ask_size:String,
    pub source_ms:i64,pub receive_ms:i64,pub sequence:i64,pub provenance:String,
}
pub fn parse_bookticker(payload:&Value,received:i64,perpetual:bool)->Result<BookTicker>{
    if !prices(payload) {return Err("malformed bookTicker prices or sizes".into())}
    let s=text(payload,"s").filter(|s|symbol_valid(s)).ok_or("invalid bookTicker symbol")?;
    let sequence=integer(payload,"u").filter(|u|*u>=0).ok_or("invalid bookTicker sequence")?;
    // Python's physical manager falls through null E to T, then permits
    // spot receive-time fallback only when both source fields are absent/null.
    let source_present=["E","T"].iter().any(|key|payload.get(*key).is_some_and(|v|!v.is_null()));
    let (source_ms,provenance)=match source(payload){Some(t)if t>0=>(t,"exchange"),None if !perpetual&&!source_present=>(received,"bn_spot_local_receive"),_=>return Err("missing perpetual exchange timestamp".into())};
    let price=|field:&str|decimal_text(&payload[field]).ok_or_else(||"malformed bookTicker prices or sizes".to_owned());
    Ok(BookTicker{symbol:s.into(),bid:price("b")?,ask:price("a")?,bid_size:price("B")?,ask_size:price("A")?,source_ms,receive_ms:received,sequence,provenance:provenance.into()})
}

#[cfg(test)]
mod selection_snapshot_tests {
    use super::*;
    fn caches(selector:&Selector)->Vec<Value> {
        selector.routes.iter().map(|r|{
            let pending:Vec<_>=r.pending.iter().map(|(payload,membership,serial,clock)|json!({"payload":payload,"membership":membership,"serial":serial,"wall_ms":clock.wall_ms,"monotonic_ms":clock.monotonic_ms})).collect();
            json!({"route":r.route,"slot":r.slot,"token":r.token,"generation":r.generation,"connected":r.connected,"replacement":r.replacement,"applied":r.applied,"serial":r.serial,"synced_serial":r.synced_serial,"quotes":r.quotes,"high":r.high,"pending":pending})
        }).collect()
    }
    fn same(reference:&Selector,thin:&Selector) {
        // Every production field is covered below, including skipped Route caches.
        // Only test reference flags, route_state_constructions, reference_clone_count
        // and selection_quote_clones are excluded; call/sweep traces still match.
        assert_eq!(reference.state(),thin.state());assert_eq!(reference.diagnostics(),thin.diagnostics());assert_eq!(caches(reference),caches(thin));
        assert_eq!(reference.stream,thin.stream);assert_eq!(reference.mode,thin.mode);assert_eq!(reference.started,thin.started);assert_eq!(reference.stopped,thin.stopped);assert_eq!(reference.targets,thin.targets);assert_eq!(reference.epochs,thin.epochs);assert_eq!(reference.available,thin.available);assert_eq!(reference.outbox,thin.outbox);assert_eq!(reference.clock.map(|c|(c.wall_ms,c.monotonic_ms)),thin.clock.map(|c|(c.wall_ms,c.monotonic_ms)));
        assert_eq!(reference.selection_calls,thin.selection_calls);assert_eq!(reference.poll_sweeps,thin.poll_sweeps);assert_eq!(reference.first_failure,thin.first_failure);assert_eq!(reference.coverage,thin.coverage);assert_eq!(reference.gap_fenced,thin.gap_fenced);assert_eq!(reference.sweep_due_ms,thin.sweep_due_ms);assert_eq!(reference.sweep_states,thin.sweep_states);
    }
    fn action(reference:&mut Selector,thin:&mut Selector,row:Value)->CompactOutcome {
        let expected=reference.apply_compact(serde_json::from_value(row.clone()).unwrap());let actual=thin.apply_compact(serde_json::from_value(row).unwrap());assert_eq!(expected,actual);same(reference,thin);actual.unwrap()
    }
    fn route(id:&str,slot:u8,token:&str,generation:u64,replacement:bool,symbols:&[String])->Value {
        json!({"type":"route","route":id,"slot":slot,"token":token,"generation":generation,"connected":true,"replacement":replacement,"applied":symbols})
    }
    fn raw(id:&str,token:&str,generation:u64,symbol:&str,sequence:u64,membership:u64,serial:u64,mono:i64)->Value {
        json!({"type":"raw","route":id,"membership":membership,"serial":serial,"wall_ms":1800000000000_i64+mono,"monotonic_ms":mono,"payload":{"s":symbol,"u":sequence,"b":"100","a":"101","B":"2","A":"3","E":1800000000000_i64+mono,"run_time_ms":1800000000000_i64+mono,"_ptk_connection_id":token,"_ptk_connection_generation":generation}})
    }
    fn poll(mono:i64)->Value {json!({"type":"poll","output_capacity":4096,"wall_ms":1800000000000_i64+mono,"monotonic_ms":mono})}
    fn gate(frame:Value,mono:i64)->Value {json!({"type":"gate","frame":frame,"wall_ms":1800000000000_i64+mono,"monotonic_ms":mono})}
    fn poll_state_pair(mode:&str,count:usize)->(Selector,Selector,Vec<String>) {
        let symbols:Vec<_>=(0..count).map(|i|format!("S{i}USDT")).collect();
        let mut eager=Selector::default();eager.reference_eager_poll=true;let mut borrowed=Selector::default();
        action(&mut eager,&mut borrowed,json!({"type":"configure","stream_id":"poll-state","mode":mode,"symbols":symbols,"wall_ms":1800000000000_i64,"monotonic_ms":0}));
        action(&mut eager,&mut borrowed,route("r0",0,"t0",1,false,&symbols));
        if mode!="single" {action(&mut eager,&mut borrowed,route("r1",1,"t1",1,false,&symbols));}
        (eager,borrowed,symbols)
    }
    fn selected_quote_pair(mode:&str,count:usize)->(Selector,Selector,Vec<String>) {
        let (mut eager,borrowed,symbols)=poll_state_pair(mode,count);
        eager.reference_eager_poll=false;eager.reference_eager_select=true;
        (eager,borrowed,symbols)
    }
    fn select_pair(eager:&mut Selector,borrowed:&mut Selector,clock:Clock,publish:bool,scope:Option<&str>) {
        eager.select(clock,publish,scope);borrowed.select(clock,publish,scope);same(eager,borrowed);
    }
    fn eager_scoped_snapshot(selector:&Selector,symbol:&str)->Value {
        // Original eager row construction, independent of state_row/state_for_symbol.
        let symbols:BTreeMap<_,_>=selector.targets.iter().map(|s|(s,json!({"epoch":selector.epochs[s],"available":selector.available.get(s).copied().unwrap_or(false),"delivery_pending":selector.staged.get(s).is_some_and(|h|update(h)>*selector.gated.get(s).unwrap_or(&-1))}))).collect();
        let mut state=json!({"stream_id":selector.stream,"generation":1,"connected":selector.started&&!selector.stopped,"logical":true,"symbols":symbols});let row=state["symbols"][symbol].clone();
        state["symbols"]=if row.is_null(){json!({})}else{json!({symbol:row})};
        state["symbol_scope"]=json!(symbol);state
    }
    fn scoped_snapshot_signature(selector:&Selector)->Value {
        json!({"state":selector.state(),"diagnostics":selector.diagnostics(),"routes":caches(selector),
            "mode":selector.mode,"targets":selector.targets,"epochs":selector.epochs,"available":selector.available,
            "started":selector.started,"stopped":selector.stopped,"outbox":selector.outbox,
            "coverage":selector.coverage,"gap_fenced":selector.gap_fenced,
            "clock":selector.clock.map(|c|(c.wall_ms,c.monotonic_ms)),"sweep_due_ms":selector.sweep_due_ms,"sweep_states":selector.sweep_states,
            "route_state_constructions":selector.route_state_constructions.get(),"poll_sweeps":selector.poll_sweeps,"selection_calls":selector.selection_calls,
            "reference_eager_poll":selector.reference_eager_poll,"reference_deep_clone":selector.reference_deep_clone,"reference_clone_count":selector.reference_clone_count,
            "reference_eager_select":selector.reference_eager_select,"selection_quote_clones":selector.selection_quote_clones})
    }
    fn assert_scoped_snapshot(selector:&Selector) {
        let before=scoped_snapshot_signature(selector);
        for symbol in ["S0USDT","S1USDT","UNKNOWN",""] {
            let expected=eager_scoped_snapshot(selector,symbol);let actual=selector.state_for_symbol(symbol);
            assert_eq!(expected,actual);assert_eq!(serde_json::to_vec(&expected).unwrap(),serde_json::to_vec(&actual).unwrap());
        }
        assert_eq!(before,scoped_snapshot_signature(selector));
    }
    fn assert_reset_readonly(selector:&Selector) {
        let before=scoped_snapshot_signature(selector);
        for symbol in selector.members.keys().map(String::as_str).chain(["UNKNOWN",""]) {
            let expected=selector.symbol_control(symbol).map(|s|(s.epoch,s.available));
            assert_eq!(expected,selector.reset_control(symbol).map(|s|(s.epoch,s.available)));
        }
        assert_eq!(before,scoped_snapshot_signature(selector));
    }
    #[test]
    fn reset_control_matches_symbol_control_without_private_mutation() {
        assert_reset_readonly(&Selector::default());
        for mode in ["single","shadow","redundant"] {
            let mut selector=Selector::default();
            let symbols=vec!["S0USDT".to_owned(),"S1USDT".to_owned()];
            let mut apply=|row:Value| {
                let outcome=selector.apply_compact(serde_json::from_value(row).unwrap()).unwrap();
                assert_reset_readonly(&selector);outcome
            };
            apply(json!({"type":"configure","stream_id":"reset-control","mode":mode,"symbols":symbols,"wall_ms":1800000000000_i64,"monotonic_ms":0}));
            apply(route("r0",0,"t0",1,false,&symbols));
            for symbol in &symbols {apply(raw("r0","t0",1,symbol,1,1,0,0));}
            let frames=apply(poll(0)).frames;
            assert_eq!(frames.len(),2);
            for frame in frames {assert_eq!(apply(gate(frame,0)).accepted,Some(true));}
            // Expiry, disconnect/reconnect and generation fencing are real actions.
            apply(poll(1001));
            let mut disconnected=route("r0",0,"t0",1,false,&symbols);disconnected["connected"]=json!(false);
            apply(disconnected);apply(poll(1002));
            apply(route("r0",0,"t2",2,false,&symbols));
            apply(raw("r0","t0",1,"S0USDT",2,1,0,1003));apply(poll(1003));
            apply(raw("r0","t2",2,"S0USDT",3,1,0,1004));
            for frame in apply(poll(1004)).frames {apply(gate(frame,1004));}
            apply(json!({"type":"targets","symbols":["S1USDT"]}));
            apply(json!({"type":"targets","symbols":symbols}));
            apply(raw("r0","t2",2,"S0USDT",4,3,0,1005));
            for frame in apply(poll(1005)).frames {apply(gate(frame,1005));}
            apply(json!({"type":"gap","route":"r0"}));apply(poll(1006));
            apply(json!({"type":"retire","route":"r0"}));apply(poll(1007));
            apply(json!({"type":"close"}));
            drop(apply);
            assert!(selector.apply_compact(Action::Poll{output_capacity:4096,clock:Clock{wall_ms:1800000001008_i64,monotonic_ms:1008}}).is_err());
            assert_reset_readonly(&selector);
        }
    }
    #[test]
    fn scoped_snapshot_matches_eager_projection_without_private_mutation() {
        assert_scoped_snapshot(&Selector::default());
        for mode in ["single","shadow","redundant"] {
            let (mut old,mut selector,symbols)=poll_state_pair(mode,2);assert_scoped_snapshot(&selector);
            for symbol in &symbols {action(&mut old,&mut selector,raw("r0","t0",1,symbol,1,1,0,0));}
            action(&mut old,&mut selector,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));
            assert_eq!(selector.state_for_symbol("S0USDT")["symbols"]["S0USDT"]["delivery_pending"],true);assert_scoped_snapshot(&selector);
            let frames=action(&mut old,&mut selector,poll(0)).frames;
            for frame in frames {action(&mut old,&mut selector,gate(frame,0));}
            assert_eq!(selector.state_for_symbol("S0USDT")["symbols"]["S0USDT"]["delivery_pending"],false);assert_scoped_snapshot(&selector);
            for (token,generation,connected,applied) in [("retired",2,true,symbols.clone()),("t0",1,false,symbols.clone()),("t0",1,true,vec![])] {
                let mut row=route("r0",0,token,generation,false,&applied);row["connected"]=json!(connected);
                action(&mut old,&mut selector,row);action(&mut old,&mut selector,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));assert_scoped_snapshot(&selector);
            }
            action(&mut old,&mut selector,json!({"type":"targets","symbols":["S0USDT"]}));assert_scoped_snapshot(&selector);
            assert_eq!(selector.state_for_symbol("S1USDT")["symbols"],json!({}));
            action(&mut old,&mut selector,json!({"type":"targets","symbols":symbols}));assert_scoped_snapshot(&selector);
            action(&mut old,&mut selector,route("r0",0,"t0",1,false,&symbols));
            let membership=selector.members["S1USDT"];assert!(membership>1);
            action(&mut old,&mut selector,raw("r0","t0",1,"S1USDT",2,membership,0,0));
            action(&mut old,&mut selector,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));
            assert!(selector.qualified(&selector.routes[0],"S1USDT",Clock{wall_ms:1800000000000_i64,monotonic_ms:0},mode=="shadow"));
            assert_eq!(selector.state_for_symbol("S1USDT")["symbols"]["S1USDT"]["available"],true);
            assert_eq!(selector.state_for_symbol("S1USDT")["symbols"]["S1USDT"]["delivery_pending"],true);assert_scoped_snapshot(&selector);
            action(&mut old,&mut selector,json!({"type":"targets","symbols":[]}));assert_scoped_snapshot(&selector);
            action(&mut old,&mut selector,json!({"type":"targets","symbols":symbols}));
            action(&mut old,&mut selector,json!({"type":"close"}));assert_scoped_snapshot(&selector);
            let (mut old,mut failed,_)=poll_state_pair(mode,2);
            action(&mut old,&mut failed,raw("r0","t0",1,"S0USDT",1,1,0,0));
            action(&mut old,&mut failed,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));
            assert!(failed.available["S0USDT"]);assert!(failed.staged.contains_key("S0USDT"));
            let error=failed.apply_compact(Action::Snapshot{clock:Clock{wall_ms:1800000000000_i64,monotonic_ms:-1}}).unwrap_err();
            assert_eq!(error,"invalid or regressing clock");assert_eq!(failed.first_failure.as_deref(),Some(error.as_str()));
            assert!(failed.stopped);assert!(!failed.routes[0].quotes.is_empty());assert!(failed.staged.contains_key("S0USDT"));assert_scoped_snapshot(&failed);
        }
    }
    #[test]
    fn scoped_snapshot_bounded_alternating_64_target_projection_cost() {
        use std::hint::black_box;
        use std::time::Instant;
        let (_,selector,_)=poll_state_pair("redundant",64);let before=scoped_snapshot_signature(&selector);
        for _ in 0..32 {assert_eq!(eager_scoped_snapshot(&selector,"S0USDT"),selector.state_for_symbol("S0USDT"));}
        for round in 0..6 {
            let mut elapsed=[0_u128;2];
            for which in if round%2==0 {[0,1]}else{[1,0]} {
                let start=Instant::now();for _ in 0..128 {black_box(if which==0 {eager_scoped_snapshot(&selector,"S0USDT")}else{selector.state_for_symbol("S0USDT")});}elapsed[which]=start.elapsed().as_nanos();
            }
            assert_eq!(eager_scoped_snapshot(&selector,"S0USDT"),selector.state_for_symbol("S0USDT"));
            eprintln!("scoped_snapshot_selector_stage_wall targets=64 round={round} calls_each=128 old_ns={} scoped_ns={} old_first={} scope=projection_only",elapsed[0],elapsed[1],round%2==0);
        }
        assert_eq!(before,scoped_snapshot_signature(&selector));
    }
    #[test]
    fn poll_state_borrowed_matches_eager_full_cohort_transitions() {
        for mode in ["single","shadow","redundant"] {for count in [22,32] {
            let (mut eager,mut borrowed,symbols)=poll_state_pair(mode,count);
            for symbol in &symbols {action(&mut eager,&mut borrowed,raw("r0","t0",1,symbol,1,1,0,0));}
            if mode!="single" {for symbol in &symbols {action(&mut eager,&mut borrowed,raw("r1","t1",1,symbol,2,1,0,0));}}
            let old=action(&mut eager,&mut borrowed,poll(0)).frames;
            let before=borrowed.route_state_constructions.get();
            for mono in 0..20 {action(&mut eager,&mut borrowed,poll(mono));}
            assert_eq!(borrowed.route_state_constructions.get(),before);
            assert_eq!(eager.route_state_constructions.get(),before+20);
            action(&mut eager,&mut borrowed,poll(20));assert_eq!(borrowed.route_state_constructions.get(),before+1);
            for frame in &old {action(&mut eager,&mut borrowed,gate(frame.clone(),20));}
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],1,1,0,20));action(&mut eager,&mut borrowed,poll(20));
            // Non-snapshot fields must not trigger an early sweep.
            let mut changed=route("r0",0,"t0",1,true,&symbols[..count/2]);
            action(&mut eager,&mut borrowed,changed.clone());action(&mut eager,&mut borrowed,poll(21));
            assert_eq!(borrowed.route_state_constructions.get(),before+1);
            for (field,value) in [("token",json!("rotated")),("generation",json!(2)),("connected",json!(false)),("connected",json!(true))] {
                changed[field]=value;action(&mut eager,&mut borrowed,changed.clone());action(&mut eager,&mut borrowed,poll(21));
            }
            action(&mut eager,&mut borrowed,route("r0",0,"t0",1,false,&symbols));
            action(&mut eager,&mut borrowed,json!({"type":"gap","route":"r0"}));
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],3,1,0,22));action(&mut eager,&mut borrowed,poll(22));
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],4,1,1,23));
            let fresh=action(&mut eager,&mut borrowed,poll(23)).frames;
            for frame in old.iter().chain(fresh.iter()) {action(&mut eager,&mut borrowed,gate(frame.clone(),23));}
            action(&mut eager,&mut borrowed,json!({"type":"targets","symbols":symbols[..count/2]}));
            action(&mut eager,&mut borrowed,json!({"type":"targets","symbols":symbols}));action(&mut eager,&mut borrowed,poll(24));
            action(&mut eager,&mut borrowed,route("replacement",0,"new",1,true,&symbols));action(&mut eager,&mut borrowed,poll(25));
            eager.routes.reverse();borrowed.routes.reverse();same(&eager,&borrowed);action(&mut eager,&mut borrowed,poll(26));
            action(&mut eager,&mut borrowed,json!({"type":"retire","route":"r0"}));action(&mut eager,&mut borrowed,poll(27));
            action(&mut eager,&mut borrowed,route("r0",0,"aba",2,false,&symbols));action(&mut eager,&mut borrowed,poll(28));
            action(&mut eager,&mut borrowed,poll(1029));action(&mut eager,&mut borrowed,json!({"type":"close"}));
        }}
    }
    #[test]
    fn poll_state_shape_types_and_snapshot_equality() {
        let (mut eager,mut borrowed,_)=poll_state_pair("redundant",22);
        action(&mut eager,&mut borrowed,poll(0));let snapshot=eager.sweep_states.clone().unwrap();
        let mut cases=vec![Value::Null,json!([]),json!({}),snapshot.clone()];
        for value in [Value::Null,json!(1),json!([]),json!({}),json!({"stream_id":"t0","generation":1,"connected":true,"extra":0}),json!({"stream_id":"t0","generation":1.0,"connected":true}),json!({"stream_id":"t0","generation":"1","connected":true}),json!({"stream_id":"t0","generation":1,"connected":1}),json!({"stream_id":1,"generation":1,"connected":true}),json!({"stream_id":"logical","generation":1,"connected":true})] {
            let mut changed=snapshot.clone();changed["r0"]=value;cases.push(changed);
        }
        for field in ["stream_id","generation","connected"] {
            let mut missing=snapshot.clone();missing["r0"].as_object_mut().unwrap().remove(field);cases.push(missing);
        }
        let mut extra=snapshot.clone();extra["extra"]=json!({});cases.push(extra);
        for value in cases {
            eager.sweep_states=Some(value.clone());borrowed.sweep_states=Some(value);
            assert_eq!(borrowed.route_states_match(),borrowed.sweep_states.as_ref()==Some(&eager.route_states()));
            action(&mut eager,&mut borrowed,poll(1));
        }
        for selector in [&mut eager,&mut borrowed] {selector.routes[0].generation=u64::MAX;selector.sweep_states=None;}
        action(&mut eager,&mut borrowed,poll(2));assert!(borrowed.route_states_match());
        for id in ["r0","r1"] {action(&mut eager,&mut borrowed,json!({"type":"retire","route":id}));}
        action(&mut eager,&mut borrowed,poll(3));assert!(borrowed.route_states_match());
    }
    #[test]
    fn poll_state_invalid_clock_capacity_and_first_failure() {
        for invalid in [json!({"type":"poll","output_capacity":4096,"wall_ms":0,"monotonic_ms":0}),json!({"type":"poll","output_capacity":4096,"wall_ms":1800000000000_i64,"monotonic_ms":-1}),json!({"type":"poll","output_capacity":4097,"wall_ms":1800000000000_i64,"monotonic_ms":0}),poll(0)] {
            let (mut eager,mut borrowed,_)=poll_state_pair("redundant",32);action(&mut eager,&mut borrowed,poll(1));
            let expected=eager.apply_compact(serde_json::from_value(invalid.clone()).unwrap());let actual=borrowed.apply_compact(serde_json::from_value(invalid).unwrap());
            assert!(expected.is_err());assert_eq!(expected,actual);same(&eager,&borrowed);
            let retry=poll(2);assert_eq!(eager.apply_compact(serde_json::from_value(retry.clone()).unwrap()),borrowed.apply_compact(serde_json::from_value(retry).unwrap()));same(&eager,&borrowed);
        }
    }
    #[test]
    fn poll_state_bounded_alternating_metadata_cost() {
        use std::hint::black_box;
        use std::time::Instant;
        let (mut eager,mut borrowed,_)=poll_state_pair("redundant",32);action(&mut eager,&mut borrowed,poll(0));
        // Fixed input, alternating order, bounded work; no wall-speed assertion.
        for round in 0..8 {
            let mut elapsed=[0_u128;2];
            for path in if round%2==0 {[0,1]}else{[1,0]} {
                let start=Instant::now();
                for _ in 0..2000 {if path==0 {black_box(eager.sweep_states.as_ref()==Some(&black_box(eager.route_states())));}else {black_box(borrowed.route_states_match());}}
                elapsed[path]=start.elapsed().as_nanos();
            }
            eprintln!("poll_state_metadata_cost round={round} iterations=2000 eager_ns={} borrowed_ns={}",elapsed[0],elapsed[1]);
        }
        assert_eq!(eager.route_state_constructions.get(),16001);assert_eq!(borrowed.route_state_constructions.get(),1);same(&eager,&borrowed);
    }
    #[test]
    fn borrowed_route_control_matches_owned_with_private_state_and_failures(){
        for mode in ["single","shadow","redundant"] {
            let symbols:Vec<_>=(0..22).map(|i|format!("S{i}USDT")).collect();let mut owned=Selector::default();let mut borrowed=Selector::default();
            action(&mut owned,&mut borrowed,json!({"type":"configure","stream_id":"borrowed","mode":mode,"symbols":symbols,"wall_ms":1800000000000_i64,"monotonic_ms":0}));
            let base=route("r0",0,"t0",1,false,&symbols);
            let apply=|a:&mut Selector,b:&mut Selector,row:Value| {
                let Action::Route{state}=serde_json::from_value(row).unwrap() else {panic!("expected route")};let control:RouteControl=state.into();
                assert_eq!(a.apply_route_control(control.clone()),b.apply_route_control_ref(&control));same(a,b);
            };
            apply(&mut owned,&mut borrowed,base.clone());
            for symbol in &symbols {action(&mut owned,&mut borrowed,raw("r0","t0",1,symbol,1,1,0,0));}
            let frames=action(&mut owned,&mut borrowed,poll(0)).frames;assert_eq!(frames.len(),22);
            action(&mut owned,&mut borrowed,gate(frames[0].clone(),0));
            for symbol in &symbols {action(&mut owned,&mut borrowed,raw("r0","t0",1,symbol,2,1,0,0));}
            assert_eq!(owned.routes[0].pending.len(),22);
            for _ in 0..4 {apply(&mut owned,&mut borrowed,base.clone());}
            for field in ["token","generation","connected","replacement","applied"] {
                let mut changed=base.clone();changed[field]=match field {"token"=>json!("t2"),"generation"=>json!(2),"connected"=>json!(false),"replacement"=>json!(true),_=>json!(["S0USDT"])};
                action(&mut owned,&mut borrowed,changed);apply(&mut owned,&mut borrowed,base.clone());
            }
            action(&mut owned,&mut borrowed,poll(0));action(&mut owned,&mut borrowed,gate(frames[0].clone(),0));
            action(&mut owned,&mut borrowed,json!({"type":"targets","symbols":["S0USDT"]}));apply(&mut owned,&mut borrowed,base.clone());
            action(&mut owned,&mut borrowed,json!({"type":"targets","symbols":symbols}));apply(&mut owned,&mut borrowed,base.clone());
            action(&mut owned,&mut borrowed,json!({"type":"retire","route":"r0"}));apply(&mut owned,&mut borrowed,base.clone());
            action(&mut owned,&mut borrowed,json!({"type":"close"}));apply(&mut owned,&mut borrowed,base);
            assert_eq!(owned.first_failure.as_deref(),Some("selector unconfigured or stopped"));
        }
        for bad in [route("r0",2,"t",1,false,&[]),route("r0",0,"",1,false,&[]),route("r0",0,"t",0,false,&[])] {
            let mut owned=Selector::default();let mut borrowed=Selector::default();
            action(&mut owned,&mut borrowed,json!({"type":"configure","stream_id":"failure","mode":"redundant","symbols":["S0USDT"],"wall_ms":1800000000000_i64,"monotonic_ms":0}));
            let Action::Route{state}=serde_json::from_value(bad).unwrap() else {unreachable!()};let control:RouteControl=state.into();
            assert_eq!(owned.apply_route_control(control.clone()),borrowed.apply_route_control_ref(&control));same(&owned,&borrowed);
            let mut valid=control;valid.slot=0;valid.token="restored".into();valid.generation=1;
            assert_eq!(owned.apply_route_control(valid.clone()),borrowed.apply_route_control_ref(&valid));same(&owned,&borrowed);
            assert_eq!(owned.first_failure.as_deref(),Some("invalid route identity"));
        }
    }
    #[test]
    fn selected_quote_borrowed_matches_eager_transitions() {
        for mode in ["shadow","redundant"] {
            let (mut eager,mut borrowed,symbols)=selected_quote_pair(mode,2);
            for (id,token) in [("r0","t0"),("r1","t1")] {for symbol in &symbols {action(&mut eager,&mut borrowed,raw(id,token,1,symbol,1,1,0,0));}}
            let initial=action(&mut eager,&mut borrowed,poll(0)).frames;assert_eq!(initial.len(),2);
            for frame in &initial {
                assert_eq!(frame["_ptk_physical_route"],"r0");assert_eq!(frame["_ptk_symbol_epoch"],0);
                let before=(eager.selection_quote_clones,borrowed.selection_quote_clones);
                action(&mut eager,&mut borrowed,gate(frame.clone(),0));
                assert_eq!(eager.selection_quote_clones,before.0+1);assert_eq!(borrowed.selection_quote_clones,before.1);
            }
            let clock=Clock{wall_ms:1800000000000,monotonic_ms:0};
            for publish in [false,true] {
                let before=(eager.selection_quote_clones,borrowed.selection_quote_clones);
                select_pair(&mut eager,&mut borrowed,clock,publish,Some(&symbols[0]));
                assert_eq!(eager.selection_quote_clones,before.0+1);assert_eq!(borrowed.selection_quote_clones,before.1);assert!(borrowed.outbox.is_empty());
            }
            // Exercise the enqueued fallback independently of staged suppression.
            eager.staged.clear();borrowed.staged.clear();same(&eager,&borrowed);
            let before=borrowed.selection_quote_clones;
            select_pair(&mut eager,&mut borrowed,clock,true,None);assert_eq!(borrowed.selection_quote_clones,before);assert!(borrowed.staged.is_empty());
            eager.selected.clear();borrowed.selected.clear();same(&eager,&borrowed);
            select_pair(&mut eager,&mut borrowed,clock,false,None);assert!(borrowed.selected.values().all(|id|id=="r0"));
            for mono in [1000,1001] {
                action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000000000_i64+mono,"monotonic_ms":mono}));
                assert_eq!(borrowed.available[&symbols[0]],mono==1000);
                assert_eq!(action(&mut eager,&mut borrowed,gate(initial[0].clone(),mono)).accepted,Some(mono==1000));
            }
            for (id,token) in [("r0","t0"),("r1","t1")] {for symbol in &symbols {action(&mut eager,&mut borrowed,raw(id,token,1,symbol,2,1,0,1002));}}
            let fresh=action(&mut eager,&mut borrowed,poll(1002)).frames;assert_eq!(fresh.len(),2);
            action(&mut eager,&mut borrowed,json!({"type":"gap","route":"r0"}));
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],3,1,0,1003));action(&mut eager,&mut borrowed,poll(1003));
            assert_eq!(borrowed.routes[0].synced_serial,1);assert_eq!(borrowed.rejections.get("ingress_gap"),Some(&1));assert_eq!(borrowed.rejections.get("ingress_retired"),Some(&1));
            // Qualification clears quotes before the next 20ms selection sweep.
            assert_eq!(borrowed.sweep_due_ms,1022);assert!(borrowed.available[&symbols[0]]);assert_eq!(borrowed.selected[&symbols[0]],"r0");
            action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000001003_i64,"monotonic_ms":1003}));
            assert_eq!(borrowed.available[&symbols[0]],mode=="redundant");
            if mode=="redundant" {assert_eq!(borrowed.selected[&symbols[0]],"r1");}
            let mut disconnected=route("r1",1,"t1",1,false,&symbols);disconnected["connected"]=json!(false);
            action(&mut eager,&mut borrowed,disconnected);action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000001003_i64,"monotonic_ms":1003}));
            action(&mut eager,&mut borrowed,route("r0",0,"rotated",2,false,&symbols));
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],3,1,1,1004));action(&mut eager,&mut borrowed,raw("r0","rotated",2,&symbols[0],4,1,1,1004));
            let rotated=action(&mut eager,&mut borrowed,poll(1004)).frames;assert_eq!(rotated.len(),1);assert_eq!(rotated[0]["_ptk_ingress_serial"],1);assert_eq!(borrowed.rejections.get("retired_generation"),Some(&1));
            let mut disconnected=route("r0",0,"rotated",2,false,&symbols);disconnected["connected"]=json!(false);
            action(&mut eager,&mut borrowed,disconnected);action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000001005_i64,"monotonic_ms":1005}));
            action(&mut eager,&mut borrowed,route("r0",0,"rotated",2,false,&symbols));action(&mut eager,&mut borrowed,poll(1005));
            assert_eq!(action(&mut eager,&mut borrowed,gate(rotated[0].clone(),1005)).accepted,Some(false));
            action(&mut eager,&mut borrowed,json!({"type":"retire","route":"r0"}));action(&mut eager,&mut borrowed,route("r0",0,"aba",3,false,&symbols));
            action(&mut eager,&mut borrowed,raw("r0","aba",3,&symbols[0],5,1,0,1006));let aba=action(&mut eager,&mut borrowed,poll(1006)).frames;assert_eq!(aba.len(),1);
            action(&mut eager,&mut borrowed,json!({"type":"targets","symbols":symbols[1..]}));action(&mut eager,&mut borrowed,json!({"type":"targets","symbols":symbols}));
            assert_eq!(borrowed.members[&symbols[0]],3);
            action(&mut eager,&mut borrowed,raw("r0","aba",3,&symbols[0],6,1,0,1007));action(&mut eager,&mut borrowed,raw("r0","aba",3,&symbols[0],7,3,0,1007));
            let current=action(&mut eager,&mut borrowed,poll(1007)).frames;assert_eq!(current.len(),1);assert_eq!(borrowed.rejections.get("retired_subscription"),Some(&1));
            assert_eq!(action(&mut eager,&mut borrowed,gate(aba[0].clone(),1007)).accepted,Some(false));assert_eq!(action(&mut eager,&mut borrowed,gate(current[0].clone(),1007)).accepted,Some(true));
            action(&mut eager,&mut borrowed,json!({"type":"close"}));select_pair(&mut eager,&mut borrowed,clock,true,None);
        }
    }
    #[test]
    fn selected_quote_borrowed_matches_eager_pressure_and_errors() {
        for mode in ["shadow","redundant"] {
            let (mut eager,mut borrowed,symbols)=selected_quote_pair(mode,2);
            for symbol in &symbols {action(&mut eager,&mut borrowed,raw("r0","t0",1,symbol,1,1,0,0));}
            action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));assert_eq!(borrowed.outbox.len(),2);
            let frame=borrowed.outbox[0].clone();let mut full=poll(0);full["output_capacity"]=json!(0);
            assert!(action(&mut eager,&mut borrowed,full).frames.is_empty());assert_eq!(borrowed.rejections.get("output_full"),Some(&2));assert!(borrowed.available.values().all(|available|!*available));
            assert_eq!(action(&mut eager,&mut borrowed,gate(frame,0)).accepted,Some(false));
            let (mut eager,mut borrowed,symbols)=selected_quote_pair(mode,2);
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],1,1,0,0));action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));
            let frame=borrowed.outbox[0].clone();
            // Bounded synthetic queue prefill reaches the real, unchanged cap.
            for selector in [&mut eager,&mut borrowed] {selector.outbox=vec![frame.clone();CAPACITY*3].into();}same(&eager,&borrowed);
            let before=(eager.selection_quote_clones,borrowed.selection_quote_clones);
            action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],2,1,0,1));action(&mut eager,&mut borrowed,json!({"type":"snapshot","wall_ms":1800000000001_i64,"monotonic_ms":1}));
            assert_eq!(eager.selection_quote_clones,before.0+3);assert_eq!(borrowed.selection_quote_clones,before.1+1);
            assert_eq!(borrowed.rejections.get("delivery_outbox_full"),Some(&1));assert_eq!(borrowed.delivery_drops,1);assert_eq!(borrowed.epochs[&symbols[0]],1);assert_eq!(update(&borrowed.staged[&symbols[0]]),2);
            assert!(borrowed.staged[&symbols[0]]["_ptk_symbol_epoch"].is_null());assert_eq!(borrowed.outbox.len(),CAPACITY*3);assert_eq!(borrowed.outbox.front(),Some(&frame));assert_eq!(borrowed.outbox.back(),Some(&frame));
            for row in [json!({"type":"snapshot","wall_ms":0,"monotonic_ms":1}),poll(2)] {
                let expected=eager.apply_compact(serde_json::from_value(row.clone()).unwrap());let actual=borrowed.apply_compact(serde_json::from_value(row).unwrap());assert!(expected.is_err());assert_eq!(expected,actual);same(&eager,&borrowed);
            }
            assert_eq!(borrowed.first_failure.as_deref(),Some("invalid or regressing clock"));assert!(borrowed.outbox.is_empty());assert_eq!(borrowed.delivery_drops,1+(CAPACITY*3) as u64);
            for invalid in [json!({"type":"gate","frame":{},"wall_ms":1800000000000_i64,"monotonic_ms":0}),json!({"type":"poll","output_capacity":4097,"wall_ms":1800000000000_i64,"monotonic_ms":0})] {
                let (mut eager,mut borrowed,symbols)=selected_quote_pair(mode,2);action(&mut eager,&mut borrowed,raw("r0","t0",1,&symbols[0],1,1,0,0));
                let expected=eager.apply_compact(serde_json::from_value(invalid.clone()).unwrap());let actual=borrowed.apply_compact(serde_json::from_value(invalid).unwrap());assert!(expected.is_err());assert_eq!(expected,actual);same(&eager,&borrowed);
                assert_eq!(borrowed.routes[0].pending.len(),1);
            }
        }
    }
    #[test]
    fn selected_quote_bounded_alternating_22_symbol_stage_cost() {
        use std::hint::black_box;
        use std::time::Instant;
        let (mut eager,mut borrowed,symbols)=selected_quote_pair("redundant",22);
        for (id,token) in [("r0","t0"),("r1","t1")] {for symbol in &symbols {action(&mut eager,&mut borrowed,raw(id,token,1,symbol,1,1,0,0));}}
        assert_eq!(action(&mut eager,&mut borrowed,poll(0)).frames.len(),22);
        let clock=Clock{wall_ms:1800000000000,monotonic_ms:0};
        for round in 0..6 {
            let mut elapsed=[0_u128;2];let before=(eager.selection_quote_clones,borrowed.selection_quote_clones);
            for path in if round%2==0 {[0,1]}else{[1,0]} {
                let selector=if path==0 {&mut eager}else{&mut borrowed};let start=Instant::now();
                for call in 0..220 {selector.select(black_box(clock),false,Some(black_box(symbols[call%22].as_str())));}
                elapsed[path]=start.elapsed().as_nanos();
            }
            same(&eager,&borrowed);assert!(borrowed.outbox.is_empty());
            let eager_copies=eager.selection_quote_clones-before.0;let borrowed_copies=borrowed.selection_quote_clones-before.1;
            assert_eq!(eager_copies,220);assert_eq!(borrowed_copies,0);
            eprintln!("selected_quote_selector_stage_wall symbols=22 round={round} calls_each=220 eager_ns={} borrowed_ns={} eager_quote_clones={eager_copies} borrowed_quote_clones={borrowed_copies} eager_first={} allocations=unmeasured scope=select_no_publish",elapsed[0],elapsed[1],round%2==0);
        }
        assert_eq!(eager.reference_clone_count,0);assert_eq!(borrowed.reference_clone_count,0);
    }
    #[test]
    fn thin_selection_snapshot_matches_original_deep_copy_with_full_cohort_caches(){
        for count in [22,32] {for mode in ["shadow","redundant"] {
            let symbols:Vec<_>=(0..count).map(|i|format!("S{i}USDT")).collect();
            let mut reference=Selector::default();reference.reference_deep_clone=true;let mut thin=Selector::default();
            action(&mut reference,&mut thin,json!({"type":"configure","stream_id":"snapshot-parity","mode":mode,"symbols":symbols,"wall_ms":1800000000000_i64,"monotonic_ms":0}));
            action(&mut reference,&mut thin,route("r0",0,"t0",1,false,&symbols));action(&mut reference,&mut thin,route("r1",1,"t1",1,false,&symbols));
            for (id,token,sequence) in [("r0","t0",1),("r1","t1",2)] {for symbol in &symbols{action(&mut reference,&mut thin,raw(id,token,1,symbol,sequence,1,0,0));}}
            let initial=action(&mut reference,&mut thin,poll(0)).frames;assert!(!initial.is_empty());
            for frame in &initial{action(&mut reference,&mut thin,gate(frame.clone(),0));}
            for r in &reference.routes{assert_eq!(r.quotes.len(),count);assert_eq!(r.high.len(),count);assert_eq!(r.applied.len(),count);}
            for (id,token,sequence) in [("r0","t0",3),("r1","t1",4)] {for symbol in &symbols{action(&mut reference,&mut thin,raw(id,token,1,symbol,sequence,1,0,1));}}
            assert!(reference.routes.iter().all(|r|r.pending.len()==count));
            // Exercise the exact old copy while every Route still contains
            // the complete cohort's quote/highwater/applied/pending buffers.
            // No qualification is inserted before these direct select calls.
            let clock=Clock{wall_ms:1800000000001,monotonic_ms:1};let before=reference.reference_clone_count;
            reference.select(clock,false,Some(&symbols[0]));thin.select(clock,false,Some(&symbols[0]));same(&reference,&thin);
            reference.select(clock,true,None);thin.select(clock,true,None);same(&reference,&thin);
            assert!(reference.reference_clone_count>before);assert!(reference.routes.iter().all(|r|r.pending.len()==count));
            action(&mut reference,&mut thin,gate(initial[0].clone(),1));let refreshed=action(&mut reference,&mut thin,poll(1)).frames;for frame in &refreshed{action(&mut reference,&mut thin,gate(frame.clone(),1));}
            action(&mut reference,&mut thin,raw("r1","t1",1,&symbols[0],5,1,0,2));action(&mut reference,&mut thin,json!({"type":"gap","route":"r1"}));action(&mut reference,&mut thin,poll(2));
            action(&mut reference,&mut thin,route("r1",1,"reconnected",2,false,&symbols));
            for symbol in &symbols{action(&mut reference,&mut thin,raw("r1","reconnected",2,symbol,6,1,1,3));}action(&mut reference,&mut thin,poll(3));action(&mut reference,&mut thin,gate(initial[0].clone(),3));
            let kept=symbols[..count/2].to_vec();action(&mut reference,&mut thin,json!({"type":"targets","symbols":kept}));action(&mut reference,&mut thin,json!({"type":"targets","symbols":symbols}));
            action(&mut reference,&mut thin,route("replacement",0,"replacement-token",1,true,&symbols));
            for (i,symbol) in symbols.iter().enumerate(){action(&mut reference,&mut thin,raw("replacement","replacement-token",1,symbol,7,if i<count/2{1}else{3},0,4));}action(&mut reference,&mut thin,poll(4));
            action(&mut reference,&mut thin,json!({"type":"retire","route":"r0"}));action(&mut reference,&mut thin,route("replacement",0,"replacement-token",1,false,&symbols));let promoted=action(&mut reference,&mut thin,poll(5)).frames;for frame in promoted{action(&mut reference,&mut thin,gate(frame,5));}
            action(&mut reference,&mut thin,json!({"type":"snapshot","wall_ms":1800000001005_i64,"monotonic_ms":1005}));action(&mut reference,&mut thin,gate(initial[0].clone(),1005));action(&mut reference,&mut thin,json!({"type":"close"}));
            assert!(reference.reference_clone_count>count);assert_eq!(thin.reference_clone_count,0);
        }}
    }
}

#[cfg(test)]
mod numeric_boundary_tests {
    use super::*;

    #[test]
    fn decimal_acceptance_keeps_arbitrary_python_precision() {
        let valid = json!({
            "b": "0.00000000000000000000000000001",
            "a": "0.00000000000000000000000000002",
            "B": "1e-10000",
            "A": "2e-10000"
        });
        assert!(prices(&valid));

        let crossed = json!({
            "b": "1.00000000000000000000000000002",
            "a": "1.00000000000000000000000000001",
            "B": "1",
            "A": "2"
        });
        assert!(!prices(&crossed));
        assert!(prices(&json!({"b":1.0,"a":2.0,"B":3,"A":4})));
        assert!(!prices(&json!({"b":"NaN","a":"2","B":"3","A":"4"})));
    }

    #[test]
    fn extreme_clock_differences_fail_closed_without_integer_overflow() {
        assert!(age_within(1_000, 0));
        assert!(!age_within(i64::MAX, i64::MIN));
        assert!(Clock {
            wall_ms: 1,
            monotonic_ms: i64::MAX - AGE,
        }
        .valid());
        assert!(!Clock {
            wall_ms: 1,
            monotonic_ms: i64::MAX - AGE + 1,
        }
        .valid());

        let mut selector = Selector::default();
        selector
            .apply_compact(serde_json::from_value(json!({
                "type":"configure", "stream_id":"clock-boundary", "mode":"single",
                "symbols":["BTCUSDT"], "wall_ms":1800000000000_i64, "monotonic_ms":0
            }))
            .unwrap())
            .unwrap();
        let boundary_poll: Action = serde_json::from_value(json!({
            "type":"poll", "output_capacity":4096, "wall_ms":1800000000000_i64,
            "monotonic_ms":i64::MAX-AGE
        }))
        .unwrap();
        assert!(selector.apply_compact(boundary_poll).is_ok());

        let mut invalid_selector = Selector::default();
        invalid_selector
            .apply_compact(serde_json::from_value(json!({
                "type":"configure", "stream_id":"clock-boundary", "mode":"single",
                "symbols":["BTCUSDT"], "wall_ms":1800000000000_i64, "monotonic_ms":0
            }))
            .unwrap())
            .unwrap();
        let invalid_poll: Action = serde_json::from_value(json!({
            "type":"poll", "output_capacity":4096, "wall_ms":1800000000000_i64,
            "monotonic_ms":i64::MAX-AGE+1
        }))
        .unwrap();
        assert!(invalid_selector.apply_compact(invalid_poll).is_err());
    }
}

#[cfg(test)]
mod review_bookticker_authority {
    use super::*;
    const NOW:i64=1800000000000;
    fn payload()->Value {json!({"s":"ETHUSDT","u":100,"b":"100.125","a":"101.25","B":"2","A":"3","E":NOW})}

    #[test]
    fn review_numeric_bbo_accepted_decimal_text_never_panics() {
        for field in ["b","a","B","A"] {
            for perpetual in [false,true] {
                let mut row=payload();
                let expected=row[field].as_str().unwrap().to_owned();
                row[field]=serde_json::from_str(&expected).unwrap();
                assert!(prices(&row),"fixture must remain in the existing accepted-price domain");
                let caught=std::panic::catch_unwind(||parse_bookticker(&row,NOW,perpetual));
                assert!(caught.is_ok(),"accepted numeric BBO field {field} must never panic");
                let parsed=caught.unwrap().expect("existing accepted exact decimal text must convert losslessly");
                let actual=match field {"b"=>parsed.bid,"a"=>parsed.ask,"B"=>parsed.bid_size,_=>parsed.ask_size};
                assert_eq!(actual,row[field].as_number().unwrap().to_string());
            }
        }
    }
    #[test]
    fn review_absent_and_null_sources_follow_python_physical_manager() {
        for (event,trade,expected) in [(None,None,None),(Some(Value::Null),None,None),
            (None,Some(Value::Null),None),(Some(Value::Null),Some(Value::Null),None),
            (Some(Value::Null),Some(json!(NOW-1)),Some(NOW-1)),(Some(json!(NOW)),Some(json!(false)),Some(NOW))] {
            let mut row=payload();row.as_object_mut().unwrap().remove("E");
            if let Some(value)=event {row["E"]=value;}if let Some(value)=trade {row["T"]=value;}
            let spot=parse_bookticker(&row,NOW,false).unwrap();
            assert_eq!(spot.source_ms,expected.unwrap_or(NOW));
            assert_eq!(spot.provenance,if expected.is_some(){"exchange"}else{"bn_spot_local_receive"});
            assert_eq!(parse_bookticker(&row,NOW,true).is_ok(),expected.is_some());
        }
    }
    #[test]
    fn review_present_invalid_sources_never_receive_local_fallback() {
        // Python BookTickerHealth._source_time: null E may use T, but a
        // present bool/string source remains invalid rather than absent.
        for (event,trade) in [(json!(true),Value::Null),(json!("1800000000000"),Value::Null),
            (json!(false),json!(NOW)),(Value::Null,json!(true)),(Value::Null,json!("1800000000000"))] {
            for perpetual in [false,true] {
                let mut row=payload();row["E"]=event.clone();row["T"]=trade.clone();
                let result=parse_bookticker(&row,NOW,perpetual);
                assert!(result.is_err(),"invalid source must not gain local fallback: {row}");
                assert_eq!(result.unwrap_err(),"missing perpetual exchange timestamp");
            }
        }
    }
}

#[cfg(test)]
mod plain_decimal_tests {
    use super::*;
    use std::cell::Cell;
    thread_local! { static REFERENCE: Cell<bool> = const { Cell::new(false) }; }
    pub(super) fn reference_enabled() -> bool { REFERENCE.with(Cell::get) }
    struct Restore(bool);
    impl Drop for Restore {
        fn drop(&mut self) { REFERENCE.with(|flag| flag.set(self.0)); }
    }
    fn with_parser<T>(reference: bool, run: impl FnOnce() -> T) -> T {
        let _restore = Restore(REFERENCE.with(|flag| flag.replace(reference)));
        run()
    }
    const NOW: i64 = 1_800_000_000_000;
    fn payload() -> Value {
        json!({"s":"ETHUSDT","u":100,"b":"100.125","a":"101.25","B":"2","A":"3","E":NOW})
    }
    fn fields(bid: &str, ask: &str, bid_size: &str, ask_size: &str) -> Value {
        json!({"b":bid,"a":ask,"B":bid_size,"A":ask_size})
    }
    fn same_prices(row: &Value) {
        assert_eq!(with_parser(false, || prices(row)), prices_reference(row));
    }
    fn parsed(row: &Value, perpetual: bool, reference: bool) -> Result<Value> {
        with_parser(reference, || parse_bookticker(row, NOW, perpetual)
            .map(|ticker| serde_json::to_value(ticker).unwrap()))
    }
    fn same_parsed(row: &Value) {
        same_prices(row);
        for perpetual in [false, true] {
            assert_eq!(parsed(row, perpetual, false), parsed(row, perpetual, true));
        }
    }
    #[test]
    fn plain_decimal_generated_and_long_prices_match_original() {
        let mut generated = Vec::new();
        for width in 1..=3_u32 {
            for mut number in 0..3_usize.pow(width) {
                let mut digits = vec![b'0'; width as usize];
                for digit in digits.iter_mut().rev() {
                    *digit = [b'0', b'1', b'9'][number % 3]; number /= 3;
                }
                let integer = String::from_utf8(digits).unwrap();
                generated.push(integer.clone());
                generated.push(format!("00{integer}.00"));
                for point in 1..integer.len() {
                    generated.push(format!("{}.{}", &integer[..point], &integer[point..]));
                }
            }
        }
        for bid in &generated {
            for ask in &generated {
                let row = fields(bid, ask, "0001.000", "0.00100");
                assert_eq!(plain_prices(&row), Some(prices_reference(&row)));
                same_prices(&row);
            }
            for field in ["b", "a", "B", "A"] {
                let mut row = fields("1", "2", "3", "4"); row[field] = json!(bid);
                assert_eq!(plain_prices(&row), Some(prices_reference(&row)));
                same_prices(&row);
            }
        }
        for (bid, ask, expected) in [
            ("1", "1.000", true), ("0001.20", "1.20000", true),
            ("0.000001", "0.0000010", true), ("1.00001", "1.00000", false),
            ("10", "9.99999", false), ("0", "1", false), ("1", "0", false),
        ] {
            let row = fields(bid, ask, "1", "1");
            assert_eq!(prices_reference(&row), expected); same_prices(&row);
        }
        let tiny = format!("0.{}1", "0".repeat(8192));
        let huge = format!("1{}", "0".repeat(8192));
        let huge_plus = format!("1{}1", "0".repeat(8191));
        for (bid, ask) in [
            (tiny.clone(), format!("{tiny}000")),
            (huge.clone(), huge_plus.clone()), (huge_plus, huge.clone()),
            (format!("000{huge}.000"), huge),
        ] {
            let row = fields(&bid, &ask, &tiny, "1");
            assert_eq!(plain_prices(&row), Some(prices_reference(&row))); same_prices(&row);
        }
        for field in ["b", "a", "B", "A"] {
            for zero in ["0", "00", "00.000"] {
                let mut row = fields("1", "2", "3", "4"); row[field] = json!(zero);
                assert_eq!(plain_prices(&row), Some(false)); assert!(!prices_reference(&row));
            }
        }
    }
    #[test]
    fn plain_decimal_fallback_and_bookticker_outputs_match_original() {
        let numeric: Vec<Value> = ["0", "1", "1.25", "1e100", "-1", "1e-100"]
            .iter().map(|text| serde_json::from_str(text).unwrap()).collect();
        for literal in ["1e1000", "-1e1000"] {
            assert!(serde_json::from_str::<Value>(literal).is_err());
        }
        let mut cases: Vec<Value> = [
            "+1", "-1", "-0", "1e2", "1E-999999999999999999999", "1e+1000",
            " 1.25\t", "1_0.0_1", ".5", "1.", "", ".", "1..0", "1__0",
            "1e", "1e+", "1e2e3", "NaN", "sNaN", "Infinity", "inf", "-Infinity",
            "１２", "١", "1\u{00a0}", "\u{00a0}1", "1\n2", "_1", "1_",
        ].iter().map(|text| json!(text)).collect();
        cases.extend(numeric.iter().cloned());
        cases.extend([Value::Null, json!(true), json!(false), json!([]), json!({})]);
        for field in ["b", "a", "B", "A"] {
            for value in &cases {
                let mut row = payload(); row[field] = value.clone();
                assert_eq!(plain_prices(&row), None); same_parsed(&row);
                if value.is_number() {
                    assert_eq!(reject_numeric_bookticker_fields(&row).unwrap_err(),
                        "native BN bookTicker requires decimal-string prices and sizes");
                }
            }
            let mut missing = payload(); missing.as_object_mut().unwrap().remove(field);
            same_parsed(&missing);
            assert_eq!(parsed(&missing, true, false).unwrap_err(), "malformed bookTicker prices or sizes");
        }
        for row in [Value::Null, json!([]), json!(true), json!("1")] { same_parsed(&row); }
        // Error precedence and the entire serialized BookTicker remain the original caller's.
        let expected = json!({"symbol":"ETHUSDT","bid":"100.125","ask":"101.25",
            "bid_size":"2","ask_size":"3","source_ms":NOW,"receive_ms":NOW,
            "sequence":100,"provenance":"exchange"});
        assert_eq!(parsed(&payload(), true, false).unwrap(), expected);
        for (field, value, error) in [
            ("s", json!("bad"), "invalid bookTicker symbol"),
            ("u", json!(-1), "invalid bookTicker sequence"),
            ("E", json!(false), "missing perpetual exchange timestamp"),
        ] {
            let mut row = payload(); row[field] = value; same_parsed(&row);
            assert_eq!(parsed(&row, true, false).unwrap_err(), error);
        }
        for event in [None, Some(Value::Null)] {
            let mut row = payload(); row.as_object_mut().unwrap().remove("E");
            if let Some(event) = event { row["E"] = event; }
            same_parsed(&row);
            assert_eq!(parsed(&row, false, false).unwrap()["provenance"], "bn_spot_local_receive");
            assert_eq!(parsed(&row, true, false).unwrap_err(), "missing perpetual exchange timestamp");
        }
        assert!(!reference_enabled());
        with_parser(true, || { assert!(reference_enabled()); with_parser(false, || assert!(!reference_enabled())); assert!(reference_enabled()); });
        assert!(!reference_enabled());
    }
    fn private_state(selector: &Selector) -> Value {
        let routes: Vec<_> = selector.routes.iter().map(|route| {
            let pending: Vec<_> = route.pending.iter().map(|(row, membership, serial, clock)|
                json!({"payload":row,"membership":membership,"serial":serial,
                    "clock":[clock.wall_ms,clock.monotonic_ms]})).collect();
            json!({"route":route.route,"slot":route.slot,"token":route.token,"generation":route.generation,
                "connected":route.connected,"replacement":route.replacement,"applied":route.applied,
                "serial":route.serial,"synced_serial":route.synced_serial,"quotes":route.quotes,
                "high":route.high,"pending":pending})
        }).collect();
        // All production Selector and Route fields, including non-serialized caches.
        json!({"stream":selector.stream,"mode":selector.mode,"stopped":selector.stopped,
            "started":selector.started,"first_failure":selector.first_failure,"targets":selector.targets,
            "members":selector.members,"epochs":selector.epochs,"routes":routes,"available":selector.available,
            "selected":selector.selected,"observed":selector.observed,"primary":selector.primary,
            "staged":selector.staged,"enqueued":selector.enqueued,"gated":selector.gated,"outbox":selector.outbox,
            "gap_fenced":selector.gap_fenced,"coverage":selector.coverage,"rejections":selector.rejections,
            "delivery_drops":selector.delivery_drops,"ingress_drops":selector.ingress_drops,
            "clock":selector.clock.map(|clock|[clock.wall_ms,clock.monotonic_ms]),
            "sweep_due_ms":selector.sweep_due_ms,"sweep_states":selector.sweep_states})
    }
    fn same_selectors(reference: &Selector, fast: &Selector) {
        assert_eq!(private_state(reference), private_state(fast));
        assert_eq!(reference.state(), fast.state()); assert_eq!(reference.diagnostics(), fast.diagnostics());
        assert_eq!(reference.poll_sweeps, fast.poll_sweeps);
        assert_eq!(reference.selection_calls, fast.selection_calls);
        assert_eq!(reference.route_state_constructions.get(), fast.route_state_constructions.get());
        assert_eq!(reference.selection_quote_clones, fast.selection_quote_clones);
    }
    fn action(reference: &mut Selector, fast: &mut Selector, row: Value) -> CompactOutcome {
        let expected = with_parser(true, || reference.apply_compact(serde_json::from_value(row.clone()).unwrap()));
        let actual = with_parser(false, || fast.apply_compact(serde_json::from_value(row).unwrap()));
        assert_eq!(expected, actual); same_selectors(reference, fast); actual.unwrap()
    }
    fn route(id: &str, slot: u8, token: &str, generation: u64, applied: &[&str]) -> Value {
        json!({"type":"route","route":id,"slot":slot,"token":token,"generation":generation,
            "connected":true,"replacement":false,"applied":applied})
    }
    fn raw(id: &str, token: &str, generation: u64, sequence: i64, membership: u64, serial: u64, mono: i64) -> Value {
        json!({"type":"raw","route":id,"membership":membership,"serial":serial,
            "wall_ms":NOW+mono,"monotonic_ms":mono,"payload":{"s":"ETHUSDT","u":sequence,
            "b":"000100.12500","a":"101.25000","B":"0.00100","A":"0003.000",
            "E":NOW+mono,"run_time_ms":NOW+mono,"_ptk_connection_id":token,"_ptk_connection_generation":generation}})
    }
    fn poll(mono: i64) -> Value { json!({"type":"poll","output_capacity":4096,"wall_ms":NOW+mono,"monotonic_ms":mono}) }
    fn gate(frame: Value, mono: i64) -> Value { json!({"type":"gate","frame":frame,"wall_ms":NOW+mono,"monotonic_ms":mono}) }
    #[test]
    fn plain_decimal_selector_raw_poll_frame_gate_transitions_match_original() {
        for mode in ["single", "shadow", "redundant"] {
            let mut reference = Selector::default(); let mut fast = Selector::default();
            action(&mut reference, &mut fast, json!({"type":"configure","stream_id":"plain-decimal",
                "mode":mode,"symbols":["ETHUSDT"],"wall_ms":NOW,"monotonic_ms":0}));
            action(&mut reference, &mut fast, route("r0", 0, "t0", 1, &["ETHUSDT"]));
            // A second physical route exercises duplicates/conflicts even when not a contributor.
            action(&mut reference, &mut fast, route("r1", if mode == "single" { 0 } else { 1 }, "t1", 1, &["ETHUSDT"]));
            action(&mut reference, &mut fast, raw("r0", "t0", 1, 100, 1, 0, 0));
            let frames = action(&mut reference, &mut fast, poll(0)).frames;
            assert_eq!(frames.len(), 1); let first = frames[0].clone();
            assert_eq!(action(&mut reference, &mut fast, gate(first.clone(), 0)).accepted, Some(true));
            action(&mut reference, &mut fast, raw("r0", "t0", 1, 100, 1, 0, 0));
            action(&mut reference, &mut fast, poll(0));
            assert!(fast.rejections["route_duplicate_or_regression"] > 0);
            let mut conflict = raw("r1", "t1", 1, 100, 1, 0, 0); conflict["payload"]["b"] = json!("100.126");
            action(&mut reference, &mut fast, conflict); action(&mut reference, &mut fast, poll(0));
            assert!(fast.rejections["conflicting_duplicate"] > 0);
            // Preserve fallback admission and malformed rejection through genuine ingest.
            for (index, value) in [json!("+100.125"), json!("1_00.125"), serde_json::from_str("100.125").unwrap(), json!("0"), json!("102")].into_iter().enumerate() {
                let mono = index as i64 + 1;
                let mut row = raw("r0", "t0", 1, 101 + index as i64, 1, 0, mono);
                row["payload"]["b"] = value;
                action(&mut reference, &mut fast, row);
                for frame in action(&mut reference, &mut fast, poll(mono)).frames {
                    assert_eq!(action(&mut reference, &mut fast, gate(frame, mono)).accepted, Some(true));
                }
            }
            assert!(fast.rejections["malformed"] >= 2);
            // A stale ingress timestamp is rejected before it can alter highwater/coverage.
            let mut stale = raw("r0", "t0", 1, 200, 1, 0, 6); stale["payload"]["E"] = json!(NOW-1001);
            action(&mut reference, &mut fast, stale); action(&mut reference, &mut fast, poll(6));
            assert!(fast.rejections["timestamp"] > 0);
            action(&mut reference, &mut fast, route("r0", 0, "t2", 2, &["ETHUSDT"]));
            assert_eq!(action(&mut reference, &mut fast, gate(first, 7)).accepted, Some(false));
            action(&mut reference, &mut fast, raw("r0", "t0", 1, 201, 1, 0, 7));
            action(&mut reference, &mut fast, poll(7)); assert!(fast.rejections["retired_generation"] > 0);
            action(&mut reference, &mut fast, raw("r0", "t2", 2, 202, 1, 0, 8));
            for frame in action(&mut reference, &mut fast, poll(8)).frames {
                action(&mut reference, &mut fast, gate(frame, 8));
            }
            action(&mut reference, &mut fast, json!({"type":"targets","symbols":[]}));
            action(&mut reference, &mut fast, json!({"type":"targets","symbols":["ETHUSDT"]}));
            action(&mut reference, &mut fast, route("r0", 0, "t2", 2, &[]));
            action(&mut reference, &mut fast, poll(9));
            action(&mut reference, &mut fast, route("r0", 0, "t2", 2, &["ETHUSDT"]));
            action(&mut reference, &mut fast, raw("r0", "t2", 2, 203, 1, 0, 9));
            action(&mut reference, &mut fast, poll(9)); assert!(fast.rejections["retired_subscription"] > 0);
            let membership = fast.members["ETHUSDT"];
            action(&mut reference, &mut fast, raw("r0", "t2", 2, 204, membership, 0, 10));
            let fresh_frames = action(&mut reference, &mut fast, poll(10)).frames;
            assert_eq!(fresh_frames.len(), 1);
            // A retained frame expires through both delivery gate and whole-cohort sweep.
            action(&mut reference, &mut fast, poll(1011));
            assert_eq!(action(&mut reference, &mut fast, gate(fresh_frames[0].clone(), 1011)).accepted, Some(false));
            assert!(!fast.available["ETHUSDT"]);
            action(&mut reference, &mut fast, json!({"type":"gap","route":"r0"}));
            action(&mut reference, &mut fast, raw("r0", "t2", 2, 205, membership, 0, 1012));
            action(&mut reference, &mut fast, poll(1012)); assert!(fast.rejections["ingress_retired"] > 0);
            action(&mut reference, &mut fast, raw("r0", "t2", 2, 206, membership, 1, 1013));
            for frame in action(&mut reference, &mut fast, poll(1013)).frames {
                assert_eq!(action(&mut reference, &mut fast, gate(frame, 1013)).accepted, Some(true));
            }
            action(&mut reference, &mut fast, json!({"type":"close"}));
        }
    }
    #[test]
    fn plain_decimal_bounded_alternating_prices_wall_cost() {
        use std::hint::black_box;
        use std::time::Instant;
        let plain: Vec<_> = [
            ("1", "1"), ("100", "101"), ("100.125", "101.25"), ("0.1", "0.2"),
            ("0.00000001", "0.00000002"), ("99999.99", "100000"), ("0001", "1.000"),
            ("1.2", "1.20000"), ("123456789012345678901234567890", "123456789012345678901234567891"),
            ("0.000000000000000000000000001", "0.000000000000000000000000002"),
            ("2", "1"), ("0", "1"), ("1", "0"), ("100.0001", "100.0000"),
            ("42.010000", "42.020000"), ("50000.00000001", "50000.00000002"),
            ("0.009", "0.01"), ("9.999", "10"), ("000.0010", "0.001"),
            ("1.00000000000000000000000001", "1.00000000000000000000000002"),
            ("800000000", "800000000.01"), ("0.00010", "0.00011"),
        ].iter().map(|(bid, ask)| fields(bid, ask, "0.00125000", "100.00000")).collect();
        let fallback: Vec<_> = ["+1", "1e-2", " 1 ", "1_0", ".5", "1.", "NaN", "-1"]
            .iter().map(|bid| fields(bid, "20", "1", "1")).collect();
        assert_eq!(plain.len(), 22);
        for (cohort, rows) in [(0, &plain), (1, &fallback)] {
            for row in rows {
                assert_eq!(plain_prices(row).is_some(), cohort == 0); same_prices(row);
            }
            let calls = 8192_usize;
            let expected: usize = (0..calls).map(|index| usize::from(prices_reference(&rows[index % rows.len()]))).sum();
            for round in 0..4 {
                let mut elapsed = [0_u128; 2]; let mut accepted = [0_usize; 2];
                for which in if round % 2 == 0 { [0, 1] } else { [1, 0] } {
                    let validator: fn(&Value) -> bool = if which == 0 { prices_reference } else { prices };
                    let start = Instant::now();
                    for index in 0..calls {
                        accepted[which] += usize::from(black_box(validator(black_box(&rows[index % rows.len()]))));
                    }
                    elapsed[which] = start.elapsed().as_nanos();
                }
                assert_eq!(accepted, [expected, expected]);
                eprintln!("{}", json!({"marker":"plain_decimal_prices_wall_v1","cohort":cohort,
                    "round":round,"calls_each":calls,"payloads":rows.len(),"reference_first":round%2==0,
                    "reference_ns":elapsed[0],"fast_ns":elapsed[1],"accepted_each":expected}));
            }
        }
    }
}


#[cfg(test)]
mod immutable_quote_tests {
    use super::*;
    use std::cell::Cell;
    thread_local! {
        static EAGER: Cell<bool> = const { Cell::new(false) };
        static OWNED_COPIES: Cell<u64> = const { Cell::new(0) };
        static ARC_SHARES: Cell<u64> = const { Cell::new(0) };
    }
    pub(super) fn eager_enabled() -> bool { EAGER.with(Cell::get) }
    pub(super) fn record_owned() { OWNED_COPIES.with(|count| count.set(count.get()+1)); }
    pub(super) fn record_share() { ARC_SHARES.with(|count| count.set(count.get()+1)); }
    struct Restore(bool);
    impl Drop for Restore { fn drop(&mut self) { EAGER.with(|flag| flag.set(self.0)); } }
    fn with_eager<T>(eager: bool, run: impl FnOnce() -> T) -> T {
        let _restore=Restore(EAGER.with(|flag| flag.replace(eager))); run()
    }
    fn reset_counts() { OWNED_COPIES.with(|count|count.set(0)); ARC_SHARES.with(|count|count.set(0)); }
    fn counts() -> [u64;2] { [OWNED_COPIES.with(Cell::get),ARC_SHARES.with(Cell::get)] }
    const NOW:i64=1_800_000_000_000;
    fn clock(mono:i64)->Clock { Clock{wall_ms:NOW+mono,monotonic_ms:mono} }
    fn configure(mode:&str,symbols:&[String])->Value {
        json!({"type":"configure","stream_id":"immutable-quote","mode":mode,"symbols":symbols,
            "wall_ms":NOW,"monotonic_ms":0})
    }
    fn route(id:&str,slot:u8,token:&str,generation:u64,symbols:&[String])->Value {
        json!({"type":"route","route":id,"slot":slot,"token":token,"generation":generation,
            "connected":true,"replacement":false,"applied":symbols})
    }
    fn payload(symbol:&str,token:&str,generation:u64,sequence:i64,mono:i64)->Value {
        json!({"s":symbol,"u":sequence,"b":"100.125","a":"101.25","B":"2","A":"3",
            "E":NOW+mono,"T":null,"run_time_ms":NOW+mono,"_ptk_connection_id":token,
            "_ptk_connection_generation":generation,"extra":{"retained":[null,true,"exact"]}})
    }
    fn captured(id:&str,token:&str,generation:u64,symbol:&str,sequence:i64,membership:u64,serial:u64,ingress:i64,now:i64)->Value {
        json!({"type":"raw_captured","route":id,"payload":payload(symbol,token,generation,sequence,ingress),
            "membership":membership,"serial":serial,"ingress_clock":{"wall_ms":NOW+ingress,"monotonic_ms":ingress},
            "wall_ms":NOW+now,"monotonic_ms":now})
    }
    fn poll(mono:i64)->Value { json!({"type":"poll","output_capacity":CAPACITY,"wall_ms":NOW+mono,"monotonic_ms":mono}) }
    fn gate(frame:Value,mono:i64)->Value { json!({"type":"gate","frame":frame,"wall_ms":NOW+mono,"monotonic_ms":mono}) }
    fn complete(selector:&Selector)->Value {
        let routes:Vec<_>=selector.routes.iter().map(|route| {
            let pending:Vec<_>=route.pending.iter().map(|(value,member,serial,clock)|
                json!([value,member,serial,clock.wall_ms,clock.monotonic_ms])).collect();
            json!({"route":route.route,"slot":route.slot,"token":route.token,"generation":route.generation,
                "connected":route.connected,"replacement":route.replacement,"applied":route.applied,
                "serial":route.serial,"synced_serial":route.synced_serial,"quotes":route.quotes,"high":route.high,"pending":pending})
        }).collect();
        // Every Selector/Route field, including every existing test counter and flag.
        // The per-thread copy counters are reported separately from semantic state.
        json!({"stream":selector.stream,"mode":selector.mode,"stopped":selector.stopped,"started":selector.started,
            "first_failure":selector.first_failure,"targets":selector.targets,"members":selector.members,"epochs":selector.epochs,
            "routes":routes,"available":selector.available,"selected":selector.selected,"observed":selector.observed,
            "primary":selector.primary,"staged":selector.staged,"enqueued":selector.enqueued,"gated":selector.gated,"outbox":selector.outbox,
            "gap_fenced":selector.gap_fenced,"coverage":selector.coverage,"rejections":selector.rejections,
            "delivery_drops":selector.delivery_drops,"ingress_drops":selector.ingress_drops,
            "clock":selector.clock.map(|clock|[clock.wall_ms,clock.monotonic_ms]),"sweep_due_ms":selector.sweep_due_ms,
            "sweep_states":selector.sweep_states,"reference_eager_poll":selector.reference_eager_poll,
            "route_state_constructions":selector.route_state_constructions.get(),"poll_sweeps":selector.poll_sweeps,
            "selection_calls":selector.selection_calls,"reference_deep_clone":selector.reference_deep_clone,
            "reference_clone_count":selector.reference_clone_count,"reference_eager_select":selector.reference_eager_select,
            "selection_quote_clones":selector.selection_quote_clones})
    }
    fn same(eager:&Selector,shared:&Selector) {
        assert_eq!(complete(eager),complete(shared));
        assert_eq!(eager.state(),shared.state()); assert_eq!(eager.diagnostics(),shared.diagnostics());
        for symbol in eager.members.keys().map(String::as_str).chain(["UNKNOWN",""]) {
            assert_eq!(eager.symbol_control(symbol),shared.symbol_control(symbol));
            assert_eq!(eager.reset_control(symbol),shared.reset_control(symbol));
            assert_eq!(eager.state_for_symbol(symbol),shared.state_for_symbol(symbol));
        }
    }
    fn pair(eager:&mut Selector,shared:&mut Selector,row:Value)->Result<CompactOutcome> {
        let expected=with_eager(true,||eager.apply_compact(serde_json::from_value(row.clone()).unwrap()));
        let actual=with_eager(false,||shared.apply_compact(serde_json::from_value(row).unwrap()));
        assert_eq!(expected,actual); same(eager,shared); actual
    }
    fn public_pair(eager:&mut Selector,shared:&mut Selector,row:Value,diagnostics:bool)->Result<Value> {
        let expected=with_eager(true,||eager.apply_with_diagnostics(serde_json::from_value(row.clone()).unwrap(),diagnostics));
        let actual=with_eager(false,||shared.apply_with_diagnostics(serde_json::from_value(row).unwrap(),diagnostics));
        assert_eq!(expected,actual);
        if let (Ok(expected),Ok(actual))=(&expected,&actual) {
            assert_eq!(serde_json::to_vec(expected).unwrap(),serde_json::to_vec(actual).unwrap());
        }
        same(eager,shared); actual
    }
    #[test]
    fn immutable_quote_handle_clone_share_and_frame_isolation() {
        let value=payload("ETHUSDT","t0",1,100,0);
        let handle=ImmutableQuote::new(value.clone());
        assert_eq!(serde_json::to_value(&handle).unwrap(),value);
        assert_eq!(serde_json::to_vec(&handle).unwrap(),serde_json::to_vec(&value).unwrap());
        assert_eq!(handle.get("u"),value.get("u"));
        let shared=with_eager(false,||handle.share());
        assert!(Arc::ptr_eq(&handle.value,&shared.value)); assert_eq!(Arc::strong_count(&handle.value),2);
        for eager in [false,true] {
            let copy=with_eager(eager,||handle.clone());
            assert!(!Arc::ptr_eq(&handle.value,&copy.value)); assert_eq!(*copy,*handle);
            assert_eq!(Arc::strong_count(&copy.value),1);
        }
        let reference=with_eager(true,||handle.share());
        assert!(!Arc::ptr_eq(&handle.value,&reference.value)); assert_eq!(*reference,*handle);
        let mut old_frame=handle.owned(); old_frame["_ptk_symbol_epoch"]=json!(7);
        let old_bytes=serde_json::to_vec(&old_frame).unwrap();
        let mut output=shared.owned(); output["extra"]["retained"][2]=json!("changed");
        output["_ptk_symbol_epoch"]=json!(8);
        assert_eq!(serde_json::to_value(&handle).unwrap(),value);
        assert_eq!(serde_json::to_value(&shared).unwrap(),value);
        assert_eq!(serde_json::to_vec(&old_frame).unwrap(),old_bytes);
        let mut route=RouteControl{route:"r0".into(),slot:0,token:"t0".into(),generation:1,
            connected:true,replacement:false,applied:BTreeSet::from(["ETHUSDT".into()])}.into_route();
        route.quotes.insert("ETHUSDT".into(),handle.share());
        route.high.insert("ETHUSDT".into(),handle.share());
        route.pending.push_back((value.clone(),1,0,clock(0)));
        let copied=with_eager(false,||route.clone());
        assert!(!Arc::ptr_eq(&route.quotes["ETHUSDT"].value,&copied.quotes["ETHUSDT"].value));
        assert!(!Arc::ptr_eq(&route.high["ETHUSDT"].value,&copied.high["ETHUSDT"].value));
        assert_eq!(route.pending[0].0,copied.pending[0].0);
        assert!(!std::ptr::eq(route.pending[0].0.as_object().unwrap(),copied.pending[0].0.as_object().unwrap()));
        assert_eq!(serde_json::to_value(&route).unwrap(),json!({"route":"r0","slot":0,"token":"t0",
            "generation":1,"connected":true,"replacement":false,"applied":["ETHUSDT"]}));
        assert!(!eager_enabled());
        with_eager(true,||{assert!(eager_enabled());with_eager(false,||assert!(!eager_enabled()));assert!(eager_enabled());});
        assert!(!eager_enabled());
        let unwind=std::panic::catch_unwind(||with_eager(true,||panic!("RAII restoration probe")));
        assert!(unwind.is_err()); assert!(!eager_enabled());
    }
    #[test]
    fn immutable_quote_captured_poll_gate_complete_transition_parity() {
        let symbols=vec!["ETHUSDT".to_owned()];
        for mode in ["single","shadow","redundant"] {
            let (mut eager,mut shared)=(Selector::default(),Selector::default());
            pair(&mut eager,&mut shared,configure(mode,&symbols)).unwrap();
            pair(&mut eager,&mut shared,route("r0",0,"t0",1,&symbols)).unwrap();
            pair(&mut eager,&mut shared,route("r1",if mode=="single"{0}else{1},"t1",1,&symbols)).unwrap();
            pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",100,1,0,0,0)).unwrap();
            let first=pair(&mut eager,&mut shared,poll(0)).unwrap().frames;
            assert_eq!(first.len(),1);
            assert_eq!(pair(&mut eager,&mut shared,gate(first[0].clone(),0)).unwrap().accepted,Some(true));
            pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",100,1,0,0,0)).unwrap();
            pair(&mut eager,&mut shared,poll(0)).unwrap();
            let mut conflict=captured("r1","t1",1,"ETHUSDT",100,1,0,0,0); conflict["payload"]["b"]=json!("100.126");
            pair(&mut eager,&mut shared,conflict).unwrap(); pair(&mut eager,&mut shared,poll(0)).unwrap();
            for stamp in [NOW+1,NOW-1001] {
                let mut boundary=captured("r0","t0",1,"ETHUSDT",101,1,0,0,0);boundary["payload"]["E"]=json!(stamp);
                pair(&mut eager,&mut shared,boundary).unwrap();pair(&mut eager,&mut shared,poll(0)).unwrap();
            }
            assert_eq!(shared.rejections.get("route_duplicate_or_regression"),Some(&1));
            assert_eq!(shared.rejections.get("conflicting_duplicate"),Some(&1));
            assert_eq!(shared.rejections.get("timestamp"),Some(&2));
            // A real captured input exactly AGE old is delivered; AGE+1 expires it.
            pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",101,1,0,1,1001)).unwrap();
            let edge=pair(&mut eager,&mut shared,poll(1001)).unwrap().frames;assert_eq!(edge.len(),1);
            assert_eq!(pair(&mut eager,&mut shared,gate(edge[0].clone(),1001)).unwrap().accepted,Some(true));
            assert_eq!(pair(&mut eager,&mut shared,gate(edge[0].clone(),1002)).unwrap().accepted,Some(false));
            pair(&mut eager,&mut shared,route("r0",0,"rotated",2,&symbols)).unwrap();
            pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",102,1,0,1003,1003)).unwrap();
            pair(&mut eager,&mut shared,poll(1003)).unwrap();
            pair(&mut eager,&mut shared,captured("r0","rotated",2,"ETHUSDT",103,1,0,1004,1004)).unwrap();
            let recovered=pair(&mut eager,&mut shared,poll(1004)).unwrap().frames;assert_eq!(recovered.len(),1);
            assert_eq!(pair(&mut eager,&mut shared,gate(recovered[0].clone(),1004)).unwrap().accepted,Some(true));
            pair(&mut eager,&mut shared,json!({"type":"targets","symbols":[]})).unwrap();
            pair(&mut eager,&mut shared,json!({"type":"targets","symbols":symbols})).unwrap();
            assert_eq!(shared.members["ETHUSDT"],3);
            for (sequence,member) in [(104,1),(105,3)] {
                pair(&mut eager,&mut shared,captured("r0","rotated",2,"ETHUSDT",sequence,member,0,1005,1005)).unwrap();
            }
            let member_frames=pair(&mut eager,&mut shared,poll(1005)).unwrap().frames;assert_eq!(member_frames.len(),1);
            assert_eq!(pair(&mut eager,&mut shared,gate(recovered[0].clone(),1005)).unwrap().accepted,Some(false));
            assert_eq!(pair(&mut eager,&mut shared,gate(member_frames[0].clone(),1005)).unwrap().accepted,Some(true));
            pair(&mut eager,&mut shared,json!({"type":"gap","route":"r0"})).unwrap();
            pair(&mut eager,&mut shared,captured("r0","rotated",2,"ETHUSDT",106,3,0,1006,1006)).unwrap();
            pair(&mut eager,&mut shared,poll(1006)).unwrap();
            pair(&mut eager,&mut shared,captured("r0","rotated",2,"ETHUSDT",107,3,1,1007,1007)).unwrap();
            let serial_frames=pair(&mut eager,&mut shared,poll(1007)).unwrap().frames;assert_eq!(serial_frames.len(),1);
            assert_eq!(pair(&mut eager,&mut shared,gate(serial_frames[0].clone(),1007)).unwrap().accepted,Some(true));
            pair(&mut eager,&mut shared,json!({"type":"retire","route":"r0"})).unwrap();
            pair(&mut eager,&mut shared,route("r0",0,"aba",3,&symbols)).unwrap();
            pair(&mut eager,&mut shared,captured("r0","aba",3,"ETHUSDT",108,3,0,1008,1008)).unwrap();
            let aba=pair(&mut eager,&mut shared,poll(1008)).unwrap().frames;assert_eq!(aba.len(),1);
            assert_eq!(pair(&mut eager,&mut shared,gate(aba[0].clone(),1008)).unwrap().accepted,Some(true));
            assert!(shared.rejections["retired_generation"]>0);assert!(shared.rejections["retired_subscription"]>0);
            assert!(shared.rejections["ingress_retired"]>0);assert!(shared.rejections["ingress_gap"]>0);
            pair(&mut eager,&mut shared,json!({"type":"close"})).unwrap();
        }
    }

    #[test]
    fn immutable_quote_default_json_errors_full_and_closed_safety() {
        let symbols=vec!["ETHUSDT".to_owned()];
        let (mut eager,mut shared)=(Selector::default(),Selector::default());
        let row=configure("single",&symbols);
        let expected=with_eager(true,||eager.apply(serde_json::from_value(row.clone()).unwrap())).unwrap();
        let actual=with_eager(false,||shared.apply(serde_json::from_value(row).unwrap())).unwrap();
        assert_eq!(expected,actual);assert_eq!(serde_json::to_vec(&expected).unwrap(),serde_json::to_vec(&actual).unwrap());
        assert_eq!(actual,json!({"type":"result","action":"configure","accepted":null,"frames":[],
            "state":{"stream_id":"immutable-quote","generation":1,"connected":true,"logical":true,
                "symbols":{"ETHUSDT":{"epoch":0,"available":false,"delivery_pending":false}}},
            "diagnostics":{"first_failure":null,"rejections":{},"delivery_drops":0,"ingress_drops":0,
                "delivery_outbox_depth":0,"observed":{},"primary":{},"staged":{},"enqueued":{},"gated":{},
                "memberships":{"ETHUSDT":1},"selected":{},"routes":[]}}));same(&eager,&shared);
        public_pair(&mut eager,&mut shared,route("r0",0,"t0",1,&symbols),true).unwrap();
        let raw=captured("r0","t0",1,"ETHUSDT",100,1,0,0,0);
        assert!(public_pair(&mut eager,&mut shared,raw,false).unwrap()["diagnostics"].is_null());
        let result=public_pair(&mut eager,&mut shared,poll(0),true).unwrap();
        let mut exact=payload("ETHUSDT","t0",1,100,0);
        for (key,value) in [("_ptk_ingress_monotonic_ms",json!(0)),("_ptk_ingress_wall_ms",json!(NOW)),
            ("_ptk_membership_version",json!(1)),("_ptk_logical_feed_id",json!("immutable-quote")),
            ("_ptk_symbol_epoch",json!(0)),("_ptk_physical_route",json!("r0")),("_ptk_ingress_serial",json!(0))] {
            exact[key]=value;
        }
        assert_eq!(result["frames"],json!([exact.clone()]));
        assert_eq!(public_pair(&mut eager,&mut shared,gate(exact.clone(),0),true).unwrap()["accepted"],true);
        // Unknown payload metadata, null T and the accepted numeric parser domain survive.
        let numeric=json!({"s":"ETHUSDT","u":100,"b":1.25,"a":2.5,"B":3,"A":4,"E":null,"T":NOW});
        assert!(parse_bookticker(&numeric,NOW,true).is_ok());
        assert_eq!(reject_numeric_bookticker_fields(&numeric).unwrap_err(),"native BN bookTicker requires decimal-string prices and sizes");
        for bad in [json!({"type":"unknown"}),json!({"type":"poll","output_capacity":null,"wall_ms":NOW,"monotonic_ms":0}),
            json!({"type":"raw_captured","route":"r0","payload":{},"membership":-1,"serial":0,
                "ingress_clock":{"wall_ms":NOW,"monotonic_ms":0},"wall_ms":NOW,"monotonic_ms":0})] {
            assert!(serde_json::from_value::<Action>(bad).is_err());
        }
        public_pair(&mut eager,&mut shared,json!({"type":"close"}),true).unwrap();
        assert_eq!(public_pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",101,1,0,0,0),true).unwrap_err(),"selector unconfigured or stopped");
        assert_eq!(shared.first_failure.as_deref(),Some("selector unconfigured or stopped"));assert!(shared.stopped);
        for (bad,error) in [
            (json!({"type":"poll","output_capacity":4097,"wall_ms":NOW,"monotonic_ms":0}),"output capacity exceeds4096"),
            (json!({"type":"gate","frame":{},"wall_ms":NOW,"monotonic_ms":0}),"invalid gate symbol"),
            (json!({"type":"snapshot","wall_ms":0,"monotonic_ms":0}),"invalid or regressing clock"),
            (captured("missing","t0",1,"ETHUSDT",101,1,0,0,0),"unknown route"),
            (captured("r0","t0",1,"ETHUSDT",101,1,0,1,0),"invalid captured ingress clock"),
        ] {
            let (mut eager,mut shared)=(Selector::default(),Selector::default());
            public_pair(&mut eager,&mut shared,configure("single",&symbols),true).unwrap();
            public_pair(&mut eager,&mut shared,route("r0",0,"t0",1,&symbols),true).unwrap();
            public_pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",100,1,0,0,0),true).unwrap();
            assert_eq!(public_pair(&mut eager,&mut shared,bad,true).unwrap_err(),error);
            assert_eq!(shared.first_failure.as_deref(),Some(error)); assert!(shared.stopped);
            assert_eq!(public_pair(&mut eager,&mut shared,poll(0),false).unwrap_err(),error);
        }
        // Reach the real RawCaptured limit using actual actions, without a synthetic cache/queue prefill.
        let (mut eager,mut shared)=(Selector::default(),Selector::default());
        pair(&mut eager,&mut shared,configure("single",&symbols)).unwrap();
        pair(&mut eager,&mut shared,route("r0",0,"t0",1,&symbols)).unwrap();
        pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",100,1,0,0,0)).unwrap();
        pair(&mut eager,&mut shared,json!({"type":"snapshot","wall_ms":NOW,"monotonic_ms":0})).unwrap();
        assert_eq!(shared.outbox.len(),1);
        for sequence in 101..101+CAPACITY as i64 {
            let row=captured("r0","t0",1,"ETHUSDT",sequence,1,0,0,0);
            let expected=with_eager(true,||eager.apply_compact(serde_json::from_value(row.clone()).unwrap()));
            let actual=with_eager(false,||shared.apply_compact(serde_json::from_value(row).unwrap()));
            assert_eq!(expected,actual);assert!(actual.is_ok());
        }
        same(&eager,&shared);assert_eq!(shared.routes[0].pending.len(),CAPACITY);
        assert_eq!(pair(&mut eager,&mut shared,captured("r0","t0",1,"ETHUSDT",5000,1,0,0,0)).unwrap_err(),"captured native ingress full");
        assert_eq!(shared.routes[0].pending.len(),CAPACITY);assert!(shared.outbox.is_empty());
        assert_eq!(shared.delivery_drops,1);assert_eq!(shared.ingress_drops,0);assert!(!shared.available["ETHUSDT"]);
        assert_eq!(pair(&mut eager,&mut shared,poll(0)).unwrap_err(),"captured native ingress full");
    }
    // This reference changes only explicit share() to the original eager Value
    // copy semantics in the unchanged algorithms; it does not certify an entire
    // historical binary, CP/L2 behavior, or full-capacity public throughput.
    #[test]
    fn immutable_quote_bounded_alternating_22_symbol_component_cost() {
        use std::hint::black_box;
        use std::time::Instant;
        const CYCLES:usize=96;
        const ACTIONS:usize=CYCLES*(22+1+22);
        assert!(ACTIONS<=8192);
        let symbols:Vec<_>=(0..22).map(|index|format!("S{index}USDT")).collect();
        for round in 0..4 {
            let (mut eager,mut shared)=(Selector::default(),Selector::default());
            pair(&mut eager,&mut shared,configure("redundant",&symbols)).unwrap();
            pair(&mut eager,&mut shared,route("r0",0,"t0",1,&symbols)).unwrap();
            // Establish a steady cohort before either timed section.
            for symbol in &symbols {
                pair(&mut eager,&mut shared,captured("r0","t0",1,symbol,1,1,0,0,0)).unwrap();
            }
            let initial=pair(&mut eager,&mut shared,poll(0)).unwrap().frames;assert_eq!(initial.len(),22);
            for frame in initial {assert_eq!(pair(&mut eager,&mut shared,gate(frame,0)).unwrap().accepted,Some(true));}
            // Input creation/deserialization and comparisons are outside timing.
            let inputs:Vec<Vec<Action>>=(0..2).map(|_|(0..CYCLES).flat_map(|cycle|symbols.iter().map(move |symbol|
                Action::RawCaptured{route:"r0".into(),payload:payload(symbol,"t0",1,cycle as i64+2,0),
                    membership:1,serial:0,ingress_clock:clock(0),clock:clock(0)})).collect()).collect();
            let mut inputs:Vec<_>=inputs.into_iter().map(Some).collect();
            let mut elapsed=[0_u128;2];let mut copy_counts=[[0_u64;2];2];
            let mut outcomes:[Vec<CompactOutcome>;2]=[Vec::with_capacity(ACTIONS),Vec::with_capacity(ACTIONS)];
            for which in if round%2==0 {[0,1]}else{[1,0]} {
                let selector=if which==0 {&mut eager}else{&mut shared};
                let mut input=inputs[which].take().unwrap().into_iter();
                reset_counts();
                with_eager(which==0,|| {
                    let start=Instant::now();
                    for _ in 0..CYCLES {
                        for _ in 0..22 {
                            outcomes[which].push(black_box(selector.apply_compact(black_box(input.next().unwrap())).unwrap()));
                        }
                        let polled=selector.apply_compact(Action::Poll{output_capacity:CAPACITY,clock:clock(0)}).unwrap();
                        // Retain complete real outputs for post-timing equality. Gate
                        // receives its own Value, exactly as the owned Action API.
                        let frames=polled.frames.clone();outcomes[which].push(black_box(polled));
                        for frame in frames {
                            outcomes[which].push(black_box(selector.apply_compact(Action::Gate{frame,clock:clock(0)}).unwrap()));
                        }
                    }
                    elapsed[which]=start.elapsed().as_nanos();
                });
                copy_counts[which]=counts();assert!(input.next().is_none());
            }
            assert_eq!(outcomes[0].len(),ACTIONS);assert_eq!(outcomes[0],outcomes[1]);same(&eager,&shared);
            for cycle in outcomes[1].chunks_exact(45) {
                assert!(cycle[..22].iter().all(|outcome|outcome.action=="raw_captured"&&outcome.frames.is_empty()));
                assert_eq!(cycle[22].frames.len(),22);
                assert!(cycle[23..].iter().all(|outcome|outcome.accepted==Some(true)));
            }
            assert!(copy_counts[0][0]>copy_counts[1][0]);assert_eq!(copy_counts[0][1],0);assert!(copy_counts[1][1]>0);
            let final_state=complete(&shared);
            let output_bytes=serde_json::to_vec(&outcomes[1].iter().flat_map(|outcome|outcome.frames.iter()).collect::<Vec<_>>()).unwrap();
            let checksum=output_bytes.iter().fold(0xcbf29ce484222325_u64,|hash,byte|(hash^u64::from(*byte)).wrapping_mul(0x100000001b3));
            assert_eq!(outcomes[1].iter().map(|outcome|outcome.frames.len()).sum::<usize>(),CYCLES*22);
            eprintln!("{}",json!({"marker":"immutable_quote_component_wall_v1","round":round,"symbols":22,
                "cycles":CYCLES,"actions_each":ACTIONS,"reference_first":round%2==0,
                "reference_ns":elapsed[0],"shared_ns":elapsed[1],
                "memory_operations":{"reference_owned_value_copies":copy_counts[0][0],"reference_arc_shares":copy_counts[0][1],
                    "shared_owned_value_copies":copy_counts[1][0],"shared_arc_shares":copy_counts[1][1],"allocations":"unmeasured"},
                "checksum_fnv1a64":format!("{checksum:016x}"),"output_bytes":output_bytes.len(),
                "final_complete_state":if round==3 {final_state}else{Value::Null},
                "scope":"RawCaptured/Poll/real-frame Gate component wall time; no CPU or public-capacity ratio"}));
        }
    }
}
