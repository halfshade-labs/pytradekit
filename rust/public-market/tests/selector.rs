use ptk_public_market::{Selector,Action};
use serde_json::{json,Value};
fn apply(s:&mut Selector,v:Value)->Value{s.apply(ptk_public_market::json::parse::<Action>(v.to_string().as_bytes()).unwrap()).unwrap()}
fn configured(mode:&str)->Selector {
    let mut s=Selector::default();apply(&mut s,json!({"type":"configure","stream_id":"logical","mode":mode,"symbols":["BTCUSDT"],"wall_ms":1800000000000_i64,"monotonic_ms":0}));
    for slot in 0..2 {apply(&mut s,json!({"type":"route","route":format!("r{slot}"),"slot":slot,"token":format!("t{slot}"),"generation":1,"connected":true,"replacement":false}));}s
}
fn raw(s:&mut Selector,route:u8,u:u64){apply(s,json!({"type":"raw","route":format!("r{route}"),"wall_ms":1800000000000_i64,"monotonic_ms":0,"serial":0,"membership":1,"payload":{"s":"BTCUSDT","u":u,"E":1800000000000_i64,"b":"100","a":"101","B":"2","A":"3","run_time_ms":1800000000000_i64,"_ptk_connection_id":format!("t{route}"),"_ptk_connection_generation":1}}));}
fn poll(s:&mut Selector,age:i64,capacity:usize)->Value{apply(s,json!({"type":"poll","wall_ms":1800000000000_i64+age,"monotonic_ms":age,"output_capacity":capacity}))}
#[test]
fn staged_enqueued_and_atomic_gate_are_distinct(){
    let mut s=configured("redundant");raw(&mut s,0,1);raw(&mut s,0,2);
    let result=poll(&mut s,0,4096);assert_eq!(result["frames"].as_array().unwrap().len(),2);assert_eq!(result["state"]["symbols"]["BTCUSDT"]["delivery_pending"],true);
    let gate=apply(&mut s,json!({"type":"gate","frame":result["frames"][1],"wall_ms":1800000000000_i64,"monotonic_ms":0}));
    assert_eq!(gate["accepted"],true);assert_eq!(gate["state"]["symbols"]["BTCUSDT"]["delivery_pending"],false);
}
#[test]
fn shadow_route_cannot_takeover(){let mut s=configured("shadow");raw(&mut s,1,1);let row=poll(&mut s,0,4096);assert!(row["frames"].as_array().unwrap().is_empty());assert_eq!(row["state"]["symbols"]["BTCUSDT"]["available"],false);}
#[test]
fn queued_expired_transition_fences_epoch(){let mut s=configured("redundant");raw(&mut s,0,1);let row=poll(&mut s,1001,4096);assert!(row["frames"].as_array().unwrap().is_empty());assert_eq!(row["diagnostics"]["rejections"]["expired_transition"],1);}
#[test]
fn gap_retires_queued_serial(){let mut s=configured("redundant");raw(&mut s,0,1);apply(&mut s,json!({"type":"gap","route":"r0"}));let row=poll(&mut s,0,4096);assert_eq!(row["diagnostics"]["rejections"]["ingress_retired"],1);assert_eq!(row["state"]["symbols"]["BTCUSDT"]["epoch"],1);}
#[test]
fn output_overflow_never_claims_gate_success(){let mut s=configured("redundant");raw(&mut s,0,1);let row=poll(&mut s,0,0);assert_eq!(row["diagnostics"]["rejections"]["output_full"],1);assert_eq!(row["state"]["symbols"]["BTCUSDT"]["available"],false);assert_eq!(row["diagnostics"]["gated"],json!({}));}
#[test]
fn duplicate_json_fields_fail_closed(){assert!(ptk_public_market::json::parse::<Action>(br#"{"type":"close","type":"snapshot"}"#).is_err());}
#[test]
fn poll_cadence_retains_cached_availability_but_public_snapshot_rechecks_gap(){
    let mut s=configured("redundant");raw(&mut s,0,1);
    let before=poll(&mut s,0,4096);assert_eq!(before["state"]["symbols"]["BTCUSDT"]["available"],true);
    apply(&mut s,json!({"type":"gap","route":"r0"}));
    let cached=poll(&mut s,0,4096);
    assert_eq!(cached["state"]["symbols"]["BTCUSDT"]["available"],true);
    assert_eq!(cached["state"]["symbols"]["BTCUSDT"]["epoch"],1);
    let public=apply(&mut s,json!({"type":"snapshot","wall_ms":1800000000000_i64,"monotonic_ms":0}));
    assert_eq!(public["state"]["symbols"]["BTCUSDT"]["available"],false);
    assert_eq!(public["state"]["symbols"]["BTCUSDT"]["epoch"],1);
}
#[test]
fn final_gate_rechecks_gap_inside_sweep_interval(){
    let mut s=configured("redundant");raw(&mut s,0,1);let frame=poll(&mut s,0,4096)["frames"][0].clone();
    apply(&mut s,json!({"type":"gap","route":"r0"}));poll(&mut s,0,4096);
    let gated=apply(&mut s,json!({"type":"gate","frame":frame,"wall_ms":1800000000000_i64,"monotonic_ms":0}));
    assert_eq!(gated["accepted"],false);assert_eq!(gated["state"]["symbols"]["BTCUSDT"]["available"],false);
}

#[test]
fn captured_backlog_retains_original_age_without_regressing_processing_clock(){
    let mut s=configured("redundant");poll(&mut s,500,4096);
    apply(&mut s,json!({"type":"raw_captured","route":"r0","wall_ms":1800000000500_i64,"monotonic_ms":500,
        "ingress_clock":{"wall_ms":1800000000000_i64,"monotonic_ms":0},"membership":1,"serial":0,
        "payload":{"s":"BTCUSDT","u":1,"E":1800000000000_i64,"b":"100","a":"101","B":"2","A":"3",
        "run_time_ms":1800000000000_i64,"_ptk_connection_id":"t0","_ptk_connection_generation":1}}));
    let row=poll(&mut s,500,4096);assert_eq!(row["frames"][0]["_ptk_ingress_monotonic_ms"],0);
    assert_eq!(row["frames"][0]["run_time_ms"],1800000000000_i64);
    let gate=apply(&mut s,json!({"type":"gate","frame":row["frames"][0],"wall_ms":1800000001001_i64,"monotonic_ms":1001}));
    assert_eq!(gate["accepted"],false);
}
