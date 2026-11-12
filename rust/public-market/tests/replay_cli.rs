use ptk_public_market::MAX_LINE;
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

struct ReplayProcess(Option<Child>);
impl Drop for ReplayProcess {
    fn drop(&mut self) {
        if let Some(child) = self.0.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn run_replay(input: Vec<u8>) -> (std::process::ExitStatus, String, String) {
    let child = Command::new(env!("CARGO_BIN_EXE_public-market-replay"))
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().expect("spawn actual replay executable");
    let mut process = ReplayProcess(Some(child));
    let child = process.0.as_mut().unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let writer = std::thread::spawn(move || stdin.write_all(&input));
    let mut stdout = child.stdout.take().unwrap();
    let output = std::thread::spawn(move || {let mut text=String::new();stdout.read_to_string(&mut text).map(|_|text)});
    let mut stderr = child.stderr.take().unwrap();
    let errors = std::thread::spawn(move || {let mut text=String::new();stderr.read_to_string(&mut text).map(|_|text)});
    let deadline = Instant::now() + Duration::from_secs(3);
    let status = loop {
        if let Some(status) = process.0.as_mut().unwrap().try_wait().unwrap() {break Some(status)}
        if Instant::now() >= deadline {break None}
        std::thread::sleep(Duration::from_millis(5));
    };
    if status.is_none() {let child=process.0.as_mut().unwrap();let _=child.kill();let _=child.wait();}
    let write_result = writer.join().unwrap();
    let output = output.join().unwrap().unwrap();
    let errors = errors.join().unwrap().unwrap();
    process.0.take();
    assert!(write_result.is_ok(), "replay stdin: {write_result:?}");
    (status.expect("replay process exceeded fixture deadline"), output, errors)
}

fn configure_line(size: usize) -> Vec<u8> {
    let mut line = json!({"type":"configure","stream_id":"foundation-cli","mode":"single",
        "symbols":["BTCUSDT"],"wall_ms":1_800_000_000_000_i64,"monotonic_ms":0}).to_string().into_bytes();
    if size > 0 {assert!(line.len()<size);line.resize(size-1,b' ');}
    line.push(b'\n');line
}

#[test]
fn foundation_replay_process_accepts_configure_close_and_inclusive_line_limit() {
    for line_size in [0, MAX_LINE] {
        let mut input = configure_line(line_size);
        if line_size>0 {assert_eq!(input.len(),MAX_LINE);assert_eq!(input.last(),Some(&b'\n'));}
        input.extend_from_slice(b"{\"type\":\"close\"}\n");
        let bytes=input.len();
        let (status,output,errors)=run_replay(input);
        assert_eq!(status.code(),Some(0),"{errors}");assert!(errors.is_empty(),"{errors}");
        let rows:Vec<Value>=output.lines().map(|line|serde_json::from_str(line).unwrap()).collect();
        assert_eq!(rows.len(),3);assert_eq!(rows[0]["type"],"result");assert_eq!(rows[0]["action"],"configure");
        assert_eq!(rows[1]["type"],"result");assert_eq!(rows[1]["action"],"close");
        assert_eq!(rows[2],json!({"type":"complete","version":1,"actions":2,"bytes":bytes}));
    }
}

#[test]
fn foundation_replay_process_rejects_oversized_empty_and_illegal_json_input() {
    let oversized=configure_line(MAX_LINE+1);assert_eq!(oversized.len(),MAX_LINE+1);
    for (input,expected) in [(oversized,"input bounds exceeded"),(Vec::new(),"empty replay"),
        (b"{\"type\":\"configure\",}\n".to_vec(),"trailing comma")] {
        let (status,output,errors)=run_replay(input);
        assert_eq!(status.code(),Some(2));assert!(output.is_empty(),"unexpected replay output: {output}");
        assert!(!output.contains("\"complete\""));
        assert!(errors.starts_with("public market replay failed: "),"{errors}");
        assert!(errors.contains(expected),"expected {expected:?}: {errors}");
    }
}
