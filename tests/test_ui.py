"""Exercise the real browser script with a dependency-free DOM/HTTP harness."""
from pathlib import Path
import shutil
import subprocess
import unittest


WEB = Path(__file__).resolve().parents[1] / 'dialogue_bridge' / 'web'


@unittest.skipUnless(shutil.which('node'), 'Node.js is optional for browser-script checks')
class BrowserFlowTests(unittest.TestCase):
    def test_browser_upload_reply_and_polling_flow(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const web = process.argv[1];
let document;
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.listeners={}; this.dataset={}; this.attributes={}; this.value=''; this.files=[]; this.hidden=false; this.disabled=false; this._text=''; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text + this.children.map(child=>child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Untrusted HTML rendering is forbidden'); }
  append(...children) { for(const child of children){ if(child.parentNode)child.remove(); child.parentNode=this; this.children.push(child); } }
  replaceChildren(...children) { for(const child of this.children)child.parentNode=null; this.children=[]; this._text=''; this.append(...children); }
  remove() { if(this.parentNode){const p=this.parentNode; p.children=p.children.filter(c=>c!==this); this.parentNode=null;} }
  setAttribute(key,value) { this.attributes[key]=String(value); }
  addEventListener(type,callback) { (this.listeners[type] ||= []).push(callback); }
  focus() { document.activeElement=this; }
  reset() { for(const id of ['task-title','task-message','task-files']){const e=document.getElementById(id);e.value='';e.files=[];} }
  async emit(type) { for(const callback of this.listeners[type]||[])await callback({preventDefault(){}}); }
}
const body=new Element('body');
function find(el,id){if(el.id===id)return el;for(const child of el.children){const got=find(child,id);if(got)return got;}return null;}
document={body,hidden:false,activeElement:null,createElement:tag=>new Element(tag),getElementById:id=>find(body,id),addEventListener(){}};
const html=fs.readFileSync(web+'/index.html','utf8');
for(const match of html.matchAll(/\bid="([^"]+)"/g)){const e=new Element('div');e.id=match[1];body.append(e);}
const storage=new Map(), calls=[], intervals=[];
const location={hash:'#token=browser-test-token',pathname:'/',search:''};
let cleared=false, now=0, replyFailures=0;
let task={id:'a'.repeat(32),title:'<img src=x onerror=bad()>',data:{message:'Please inspect the upload'},files:[],status:'needs_input',thread:{thread_id:'demo-thread'},question_group:{revision:1,status:'pending',questions:[{id:'scope',title:'Which scope?',options:['One','Two']}]},events:[{id:'start-event',type:'dialogue.start',status:'delivered'}],history:[],created_at:'2026-01-01T00:00:00Z',updated_at:'2026-01-01T00:00:01Z'};
let stale=null;
function response(status,data){return {status,ok:status>=200&&status<300,async json(){return structuredClone(data);}};}
async function fetchMock(path,opts){
  assert.equal(opts.headers.Authorization,'Bearer browser-test-token');
  assert.equal(opts.redirect,'error');
  calls.push({path,body:opts.body&&JSON.parse(opts.body)});
  if(path==='/api/config')return response(200,{adapter:'demo',max_file_bytes:1024,max_total_bytes:2048});
  if(path==='/api/tasks'&&opts.method==='GET')return response(200,{tasks:[stale||task]});
  if(path==='/api/tasks'&&opts.method==='POST'){
    assert.equal(JSON.parse(opts.body).title,'A new upload');
    const key=JSON.parse(opts.body).request_key;assert.match(key,/^[a-f0-9]{32}$/);
    task={...task,title:'A new upload',data:JSON.parse(opts.body).data,updated_at:'2026-01-01T00:00:02Z'};
    return response(200,{task});
  }
  if(path.endsWith('/reply')){
    if(replyFailures++===0)throw new Error('simulated broken connection');
    const data=JSON.parse(opts.body);assert.equal(data.revision,1);assert.equal(data.answers[0].text,'A carefully written answer');
    stale=structuredClone(task);
    task={...task,status:'running',question_group:{...task.question_group,status:'answered',answers:data.answers},events:[...task.events,{id:'reply-event',type:'dialogue.reply',status:'pending'}],updated_at:'2026-01-01T00:00:03Z'};
    return response(200,{task});
  }
  if(path.startsWith('/api/tasks/'))return response(200,{task:stale||task});
  throw new Error('Unexpected API route '+path);
}
const context={document,location,sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},history:{replaceState(a,b,url){assert.equal(url,'/');cleared=true;}},crypto:require('node:crypto').webcrypto,URLSearchParams,URL,Uint8Array,Map,Set,JSON,Error,Promise,fetch:fetchMock,setTimeout,clearTimeout,setInterval:fn=>intervals.push(fn),console};
vm.runInNewContext(fs.readFileSync(web+'/app.js','utf8'),context);
const flush=async()=>{for(let i=0;i<12;i++)await new Promise(setImmediate);};
const byId=id=>document.getElementById(id);
(async()=>{
  await flush();
  assert.ok(cleared,'token fragment is removed before API activity');
  assert.equal(storage.get('dialogue-bridge-token'),'browser-test-token');
  assert.equal(byId('demo-notice').hidden,false,'demo cannot be mistaken for live AI');
  assert.ok(byId('task-detail').textContent.includes('<img src=x onerror=bad()>'),'untrusted title remains literal text');
  assert.ok(byId('task-list').textContent.includes('1 待回复'));

  byId('task-title').value='A new upload';byId('task-message').value='Summary request';
  await byId('upload-form').emit('submit');await flush();
  assert.equal(calls.filter(call=>call.path==='/api/tasks'&&call.body).length,1);
  assert.equal(byId('task-title').value,'','successful upload clears fields');

  const input=byId('answer-'+task.id+'-1-scope');
  input.value='A carefully written answer';input.focus();
  await intervals[0]();await flush();
  assert.equal(byId(input.id),input,'poll preserves the editable DOM');
  assert.equal(input.value,'A carefully written answer');
  assert.equal(document.activeElement,input,'poll preserves keyboard focus');
  const form=byId('question-host').children[0].children.find(e=>e.tagName==='form');
  await form.emit('submit');await flush();
  assert.ok(byId('question-host').textContent.includes('网络连接未完成'));
  await form.emit('submit');await flush();
  const replies=calls.filter(call=>call.path.endsWith('/reply'));
  assert.equal(replies.length,2);assert.equal(replies[0].body.request_key,replies[1].body.request_key,'uncertain browser retry uses same key');
  assert.equal(byId('question-host').children.length,0,'answered card clears before callback delivery');
  assert.ok(byId('detail-events').textContent.includes('等待投递'));
  await intervals[0]();await flush();
  assert.equal(byId('question-host').children.length,0,'an old in-flight poll cannot resurrect the reply form');

  stale=null;
  task={...task,events:[...task.events,{id:'unknown-event',type:'dialogue.reply',status:'unknown'}],updated_at:'2026-01-01T00:00:04Z'};
  await intervals[0]();await flush();
  const unknown=byId('detail-events').children.find(e=>e.className.includes('unknown'));
  assert.ok(unknown.textContent.includes('请管理员核对原对话'));
  assert.equal(unknown.children.filter(e=>e.tagName==='button').length,0,'unknown delivery has no automatic retry');

  task={...task,status:'needs_input',question_group:{revision:2,status:'pending',questions:[{id:'choice',title:'Revised question'}]},updated_at:'2026-01-01T00:00:05Z'};
  await intervals[0]();await flush();
  byId('answer-'+task.id+'-2-choice').value='Keep this draft';
  task={...task,question_group:{revision:3,status:'pending',questions:[{id:'choice',title:'Latest question'}]},updated_at:'2026-01-01T00:00:06Z'};
  await intervals[0]();await flush();
  const draft=byId('draft-host').children[0];
  assert.ok(draft.textContent.includes('已保留上一版回复草稿'));
  assert.equal(draft.children.find(e=>e.tagName==='textarea').value,'Revised question\nKeep this draft');
  assert.ok(byId('question-host').textContent.includes('Latest question'));
  console.log('browser-flow-ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ['node', '-e', script, str(WEB)], capture_output=True, text=True,
            encoding='utf-8', timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('browser-flow-ok', result.stdout)


if __name__ == '__main__':
    unittest.main()
