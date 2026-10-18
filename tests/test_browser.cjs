"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {init, validPlan} = require("../ui/app.js");
const good = () => ({product:"CultureFit",city:"Chicago",execution_mode:"LIVE_HTTP_UNREVIEWED",
 complete_required_lanes:true,provider_independently_verified:false,public_release_authorized:false,submission_authorized:false,
 resolved_seeds:[{name:"Taylor Swift",entity_id:"seed"}],
 evidence_cards:[["place","place"],["brand_partner","brand"],["artist","artist"]].map(([lane,type])=>({lane,subtype:"urn:entity:"+type,name:"<img src=x onerror=bad()>",entity_id:lane}))});
class Element {
 constructor(){this.children=[];this.listeners={};this.disabled=false;this.textContent="";this.value="";this.files=[];this.checked=false;}
 addEventListener(name,fn){this.listeners[name]=fn;}
 append(...nodes){this.children.push(...nodes);}
 replaceChildren(...nodes){this.children=[...nodes];}
 async fire(name){if(this.disabled)return;return this.listeners[name]({preventDefault(){}});}
}
const response=(status,data,type="application/json")=>({ok:status>=200&&status<300,status,headers:{get:()=>type},json:async()=>data});
function setup(options={}) {
 const elements={},calls=[];
 for(const id of ["catalog-file","replace-catalog","city","interests"])elements[id]=new Element();
 const doc={getElementById(id){return elements[id]||(elements[id]=new Element());},createElement(){return new Element();}};
 const fetcher=async(path,opts)=>{
  calls.push({path,opts});
  if(path==="/api/provider-status")return response(200,{mode:options.mode||"LIVE_HTTP_UNREVIEWED",attempted:options.attempted||false,provider_independently_verified:false});
  if(path==="/api/catalog"&&(!opts||!opts.method))return response(200,{state:options.catalogReady?"ready":"empty",record_count:3});
  if(path==="/api/plan"){if(options.plan)return options.plan();return response(200,good());}
  if(path==="/api/catalog")return response(201,{state:"ready"});
  if(path==="/api/shortlist")return response(200,options.shortlist||{provider_calls:0,source_status:"user_supplied_unverified",lanes:{place:[],brand:[],artist:[]}});
  throw Error("Unexpected route");
 };
 const app=init(doc,fetcher);return{app,elements,calls};
}
test("valid three-lane frozen result accepted",()=>assert.equal(validPlan(good()),true));
test("malformed seed and lane records fail closed",()=>{
 for(const value of [null,{}, {...good(),resolved_seeds:[null]}, {...good(),evidence_cards:[null,null,null]}, {...good(),city:"Boston"}])assert.equal(validPlan(value),false);
});
test("missing and duplicate categories rejected",()=>{
 const d=good();d.evidence_cards[2]=d.evidence_cards[0];assert.equal(validPlan(d),false);
});
test("wrong types and promoted authority flags rejected",()=>{
 const d=good();d.evidence_cards[0].subtype="urn:entity:placebo";assert.equal(validPlan(d),false);
 for(const key of ["provider_independently_verified","public_release_authorized","submission_authorized"]){const x=good();x[key]=true;assert.equal(validPlan(x),false);}
});
test("startup reads status only; no provider attempt",async()=>{
 const x=setup();await x.app.initial;assert.equal(x.elements["plan-run"].disabled,false);assert.equal(x.calls.filter(c=>c.path==="/api/plan").length,0);
});
test("locked and previously attempted sessions cannot submit",async()=>{
 for(const options of [{mode:"NETWORK_LOCKED"},{attempted:true}]){const x=setup(options);await x.app.initial;await x.elements["plan-form"].fire("submit");assert.equal(x.elements["plan-run"].disabled,true);assert.equal(x.calls.filter(c=>c.path==="/api/plan").length,0);}
});
test("single submit renders safe text and refuses a double submit",async()=>{
 const x=setup();await x.app.initial;await x.elements["plan-form"].fire("submit");await x.elements["plan-form"].fire("submit");
 assert.equal(x.calls.filter(c=>c.path==="/api/plan").length,1);assert.equal(x.elements["plan-results"].children.length,3);
 assert.equal(x.elements["plan-results"].children[0].children[1].textContent,"<img src=x onerror=bad()>");
 assert.equal(x.elements["plan-run"].disabled,true);assert.match(x.elements["plan-status"].textContent,/ready/);
 const req=x.calls.find(c=>c.path==="/api/plan");assert.equal(req.opts.body,"{}");assert.equal(req.opts.credentials,"same-origin");
});
test("in-flight double submit sends only one request",async()=>{
 let release;const wait=new Promise(resolve=>{release=resolve;});const x=setup({plan:async()=>{await wait;return response(200,good());}});
 await x.app.initial;const first=x.elements["plan-form"].fire("submit");await x.elements["plan-form"].fire("submit");release();await first;assert.equal(x.calls.filter(c=>c.path==="/api/plan").length,1);
});
test("connection interruption never automatically retries",async()=>{
 const x=setup({plan:async()=>{throw Error("private detail");}});await x.app.initial;await x.elements["plan-form"].fire("submit");await x.app.refresh();
 assert.equal(x.calls.filter(c=>c.path==="/api/plan").length,1);assert.equal(x.elements["plan-run"].disabled,true);assert.equal(x.elements["plan-results"].children.length,0);
});
test("incomplete or mismatched responses never render cards",async()=>{
 for(const data of [{...good(),evidence_cards:[]},{...good(),execution_mode:"SYNTHETIC_TRANSPORT_ONLY"}]){
  const x=setup({plan:async()=>response(200,data)});await x.app.initial;await x.elements["plan-form"].fire("submit");assert.equal(x.elements["plan-results"].children.length,0);assert.match(x.elements["plan-status"].textContent,/did not match/);
 }
});
test("provider errors have bounded messages and no retry",async()=>{
 for(const status of [409,422,503,502]){const x=setup({plan:async()=>response(status,{error:"SECRET_RAW_MESSAGE"})});await x.app.initial;await x.elements["plan-form"].fire("submit");assert.doesNotMatch(x.elements["plan-status"].textContent,/SECRET/);assert.equal(x.elements["plan-run"].disabled,true);}
});
test("synthetic results are labelled fictional",async()=>{
 const d=good();d.execution_mode="SYNTHETIC_TRANSPORT_ONLY";const x=setup({mode:d.execution_mode,plan:async()=>response(200,d)});await x.app.initial;await x.elements["plan-form"].fire("submit");
 assert.match(x.elements["result-boundary"].textContent,/fictional/);assert.match(x.elements["plan-results"].children[0].children[3].textContent,/not Qloo evidence/);
});
test("non-JSON result is refused without retry",async()=>{
 const x=setup({plan:async()=>response(200,good(),"text/html")});await x.app.initial;await x.elements["plan-form"].fire("submit");assert.equal(x.elements["plan-results"].children.length,0);assert.equal(x.elements["plan-run"].disabled,true);
});
test("catalogue overwrite requires explicit confirmation",async()=>{
 const x=setup({catalogReady:true});await x.app.initial;x.elements["catalog-file"].files=[{size:200}];await x.elements["upload-form"].fire("submit");assert.equal(x.calls.filter(c=>c.path==="/api/catalog"&&c.opts?.method).length,0);
 x.elements["replace-catalog"].checked=true;await x.elements["upload-form"].fire("submit");assert.equal(x.calls.find(c=>c.path==="/api/catalog"&&c.opts?.method).opts.headers["X-Replace-Catalogue"],"yes");
});
test("empty local lanes remain empty; catalogue query never requests provider",async()=>{
 const x=setup({catalogReady:true});await x.app.initial;x.elements.city.value="Chicago";x.elements.interests.value="jazz";await x.elements["search-form"].fire("submit");assert.equal(x.elements["catalog-results"].children.length,3);assert.equal(x.calls.filter(c=>c.path==="/api/plan").length,0);assert.match(x.elements["catalog-results"].children[0].children[1].textContent,/No matching/);
});
