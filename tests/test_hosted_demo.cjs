"use strict";
const test=require("node:test"),assert=require("node:assert/strict"),vm=require("node:vm"),fs=require("node:fs"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../ui/hosted-demo.js"),"utf8");
function setup(options={}){
 const data=new Map(),calls=[];
 const native=async(url,opts)=>{calls.push({url,opts});return new Response(JSON.stringify({state:"ready"}),{status:options.status||201,headers:{"Content-Type":"application/json"}});};
 const root={location:{hostname:options.local?"127.0.0.1":"culturefit.example"},document:{documentElement:{getAttribute:()=> "true"}},sessionStorage:{getItem:k=>{if(options.noStorage)throw Error("unavailable");return data.get(k);},setItem:(k,v)=>{if(options.noStorage)throw Error("unavailable");data.set(k,v);}},fetch:native};
 vm.runInNewContext(source,{window:root,Response,Headers});
 return{root,data,calls,native};
}
test("loopback continues to use the real application transport",()=>{const x=setup({local:true});assert.equal(x.root.fetch,x.native);});
test("hosted status is explicitly synthetic with no server call",async()=>{const x=setup();const r=await x.root.fetch("/api/provider-status");assert.equal((await r.json()).mode,"SYNTHETIC_TRANSPORT_ONLY");assert.equal(x.calls.length,0);});
test("one tab cannot automatically repeat the synthetic brief",async()=>{
 const x=setup();await x.root.fetch("/api/plan",{method:"POST",body:"{}"});assert.equal((await x.root.fetch("/api/plan",{method:"POST",body:"{}"})).status,409);
 assert.equal(x.calls.length,1);assert.equal(x.calls[0].url,"/api/demo");assert.equal(JSON.parse(x.calls[0].opts.body).body,"{}");
 assert.equal((await(await x.root.fetch("/api/provider-status")).json()).attempted,true);
});
test("no-storage mode retains a safe in-memory one-shot fence",async()=>{const x=setup({noStorage:true});await x.root.fetch("/api/plan",{method:"POST",body:"{}"});assert.equal((await x.root.fetch("/api/plan",{method:"POST",body:"{}"})).status,409);assert.equal(x.calls.length,1);});
test("validated catalogue text is supplied explicitly for the next calculation",async()=>{
 const x=setup();await x.root.fetch("/api/catalog",{method:"POST",body:{text:async()=> "CSV fixture"}});
 await x.root.fetch("/api/shortlist",{method:"POST",body:'{"city":"Chicago","interests":"jazz"}'});
 assert.equal(JSON.parse(x.calls[1].opts.body).catalogue_csv,"CSV fixture");assert.equal(x.calls[1].opts.credentials,"same-origin");
});
test("refused upload cannot replace tab catalogue state",async()=>{
 const x=setup({status:422});await x.root.fetch("/api/catalog",{method:"POST",body:{text:async()=> "invalid"}});
 await x.root.fetch("/api/catalog");assert.equal(JSON.parse(x.calls[1].opts.body).catalogue_csv,"");
});
test("replacement confirmation is forwarded as a boolean",async()=>{
 const x=setup();await x.root.fetch("/api/catalog",{method:"POST",headers:{"X-Replace-Catalogue":"yes"},body:{text:async()=> "CSV"}});
 assert.equal(JSON.parse(x.calls[0].opts.body).replace_confirmed,true);
});
