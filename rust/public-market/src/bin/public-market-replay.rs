use ptk_public_market::{Action,Selector,MAX_LINE,MAX_ACTIONS,MAX_BYTES};
use serde_json::json;
use std::io::{self,BufRead,Read,Write};
fn run()->Result<(),String>{
    let mut input=io::stdin().lock();let mut output=io::BufWriter::new(io::stdout().lock());
    let mut selector=Selector::default();let mut total=0;let mut actions=0;
    loop {
        let mut line=Vec::new();let size=input.by_ref().take((MAX_LINE+1)as u64).read_until(b'\n',&mut line).map_err(|e|e.to_string())?;
        if size==0{break}
        total+=size;actions+=1;
        if size>MAX_LINE||total>MAX_BYTES||actions>MAX_ACTIONS{return Err("input bounds exceeded".into())}
        let action:Action=ptk_public_market::json::parse(&line).map_err(|e|e.to_string())?;
        let result=selector.apply(action)?;
        writeln!(output,"{result}").map_err(|e|e.to_string())?;output.flush().map_err(|e|e.to_string())?;
    }
    if actions==0{return Err("empty replay".into())}
    writeln!(output,"{}",json!({"type":"complete","version":1,"actions":actions,"bytes":total})).map_err(|e|e.to_string())?;
    output.flush().map_err(|e|e.to_string())
}
fn main(){if let Err(error)=run(){eprintln!("public market replay failed: {error}");std::process::exit(2)}}
