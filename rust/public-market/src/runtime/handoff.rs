//! Internal opt-in seam only. No wire configuration protocol is enabled here.
use crate::Result;
use crate::transport::{SpotInstruments, SpotVenue};
use serde::Serialize;
use std::collections::BTreeMap;
use serde_json::Value;
use std::sync::Arc;

#[derive(Clone,Debug,Default,PartialEq,Eq)]
pub struct Targets {
    pub perpetual: Vec<String>,
    pub spot: Vec<String>,
    pub native_spot: BTreeMap<SpotVenue, SpotInstruments>,
}
impl Targets {pub(super) fn normalize(mut self)->Self{self.perpetual.sort();self.spot.sort();self}}

impl Targets {
    pub(super) fn applied(&self, state: &Value) -> Option<std::collections::BTreeSet<&str>> {
        if let Some(venue) = state.get("venue") {
            let venue: SpotVenue = serde_json::from_value(venue.clone()).ok()?;
            return Some(self.native_spot.get(&venue)?.keys().map(String::as_str).collect());
        }
        let symbols = if state["spot"] == true { &self.spot } else { &self.perpetual };
        Some(symbols.iter().map(String::as_str).collect())
    }
}
#[derive(Clone)]
pub(super) struct Plan {pub operation:u64,pub before:Targets,pub after:Targets}
impl Plan {
    pub(super) fn requires_ack(&self, state: &Value) -> bool {
        let Some(venue) = state.get("venue").and_then(|value| serde_json::from_value::<SpotVenue>(value.clone()).ok()) else {
            return true;
        };
        self.before.native_spot.get(&venue) != self.after.native_spot.get(&venue)
    }
}
#[derive(Clone,Copy,Debug,PartialEq,Eq,Serialize)]
#[serde(rename_all="SCREAMING_SNAKE_CASE")]
pub enum Phase {Prepared,Committed,Applying,Ready,Active,Aborted,Rejected,Failed}
#[derive(Clone,Debug,PartialEq,Eq,Serialize)]
pub struct Receipt {pub operation:u64,pub phase:Phase,pub first_failure:Option<String>}
#[derive(Default)]
pub struct Slot {current:Option<(Arc<Plan>,Receipt)>,last:Option<Receipt>,highwater:u64,acknowledged:BTreeMap<String,(String,u64)>}
impl Slot {
    pub(super) fn phase(&self)->Option<Phase>{self.current.as_ref().map(|(_,r)|r.phase)}
    pub fn status(&self)->(Option<Receipt>,Option<Receipt>){(self.current.as_ref().map(|(_,r)|r.clone()),self.last.clone())}
    pub fn targets(&self,operation:u64)->Result<Targets>{self.current.as_ref().filter(|(_,r)|r.operation==operation).map(|(p,_)|p.after.clone()).ok_or_else(||"configuration operation mismatch".into())}
    pub(super) fn first_failure(&self)->Option<String>{self.current.as_ref().and_then(|(_,r)|r.first_failure.clone())}
    pub fn fenced(&self)->bool{self.current.as_ref().is_some_and(|(_,r)|!matches!(r.phase,Phase::Prepared|Phase::Active))}
    pub(super) fn prepare(&mut self,plan:Plan)->Result<()> {
        if self.current.as_ref().is_some_and(|(p,r)|p.operation==plan.operation&&p.after==plan.after&&r.phase==Phase::Prepared){return Ok(())}
        if self.current.is_some()||plan.operation<=self.highwater{return Err("configuration slot busy or stale operation".into())}
        self.highwater=plan.operation;
        let receipt=Receipt{operation:plan.operation,phase:Phase::Prepared,first_failure:None};
        self.current=Some((Arc::new(plan),receipt));Ok(())
    }
    /// The actor supplies only same-thread logical mutations, never IO or joins.
    /// A failed partial logical mutation stays fenced, and releases no command.
    pub fn commit_logical<F>(&mut self,operation:u64,install:F)->Result<()> where F:FnOnce()->Result<()> {
        let (_,receipt)=self.current.as_mut().ok_or("missing configuration preparation")?;
        if receipt.operation!=operation{return Err("configuration commit mismatch".into())}
        if matches!(receipt.phase,Phase::Committed|Phase::Applying|Phase::Ready|Phase::Active){return Ok(())}
        if receipt.phase!=Phase::Prepared{return Err(receipt.first_failure.clone().unwrap_or_else(||"configuration commit mismatch".into()))}
        // Fence before the first logical mutation, including its failure path.
        receipt.phase=Phase::Committed;
        if let Err(error)=install(){self.fail(error.clone());return Err(error)}
        Ok(())
    }
    pub(super) fn command(&mut self)->Option<Arc<Plan>> {
        let (plan,receipt)=self.current.as_mut()?;
        if receipt.phase!=Phase::Committed{return None}
        receipt.phase=Phase::Applying;Some(plan.clone())
    }
    pub(super) fn ready(&mut self,truth:&BTreeMap<String,Value>){if let Some((plan,r))=&mut self.current{if matches!(r.phase,Phase::Applying|Phase::Ready){r.phase=Phase::Ready;self.acknowledged=truth.iter().filter(|(_,state)|plan.requires_ack(state)).map(|(id,s)|(id.clone(),(s["token"].as_str().unwrap_or_default().into(),s["generation"].as_u64().unwrap_or(0)))).collect();}}}
    pub(super) fn requires_ack(&self,state:&Value)->bool {self.current.as_ref().is_none_or(|(plan,_)|plan.requires_ack(state))}
    pub(super) fn not_ready(&mut self){if let Some((_,r))=&mut self.current{if r.phase==Phase::Ready{r.phase=Phase::Applying;self.acknowledged.clear();}}}
    pub fn revalidate(&self,before:&Targets)->Result<()> {let (plan,r)=self.current.as_ref().ok_or("missing configuration preparation")?;if r.phase!=Phase::Prepared||&plan.before!=before{return Err("configuration physical baseline changed".into())}Ok(())}
    pub fn activate(&mut self,operation:u64,truth:&BTreeMap<String,Value>)->Result<()> {
        let (plan,r)=self.current.as_mut().ok_or("missing configuration operation")?;
        if r.operation!=operation{return Err("configuration activation mismatch".into())}
        if r.phase==Phase::Active{return Ok(())}
        if r.phase!=Phase::Ready||self.acknowledged.len()!=truth.values().filter(|state|plan.requires_ack(state)).count()||!self.acknowledged.iter().all(|(id,(token,generation))|truth.get(id).is_some_and(|s|s["connected"]==true&&s["token"]==*token&&s["generation"]==*generation)){return Err("configuration is not current ACK ready".into())}
        if !truth.values().filter(|state|plan.requires_ack(state)).all(|s|{
            let applied=s["applied"].as_array().and_then(|a|a.iter().map(Value::as_str).collect::<Option<std::collections::BTreeSet<_>>>());
            applied.is_some() && applied==plan.after.applied(s)
        }){return Err("configuration ACK target mismatch".into())}
        r.phase=Phase::Active;Ok(())
    }
    pub fn finish_active(&mut self)->Result<()> {if !self.current.as_ref().is_some_and(|(_,r)|r.phase==Phase::Active){return Err("configuration not active".into())}self.last=self.current.take().map(|(_,r)|r);self.acknowledged.clear();Ok(())}
    pub fn fail_closed(&mut self,error:String){self.fail(error);}

    pub(super) fn fail(&mut self,error:String){if let Some((_,r))=&mut self.current{r.phase=Phase::Failed;if r.first_failure.is_none(){r.first_failure=Some(error.chars().take(256).collect());}}}
    /// Only a pure preparation can be discarded. READY remains fenced: activation
    /// and immutable transition-owner rules are deliberately not exposed yet.
    pub fn abort(&mut self,operation:u64)->Result<()> {
        if !self.current.as_ref().is_some_and(|(_,r)|r.operation==operation&&r.phase==Phase::Prepared){return Err("configuration abort after commit or mismatch".into())}
        self.last=self.current.take().map(|(_,mut r)|{r.phase=Phase::Aborted;r});Ok(())
    }
}
