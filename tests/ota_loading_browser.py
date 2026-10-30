"""Render the shipped OTA page through each async phase; no device/network I/O."""
from pathlib import Path
import argparse
import base64
from playwright.sync_api import sync_playwright

TOP=Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser();parser.add_argument('--browser');args=parser.parse_args()
resources=TOP/'openwrt/luci-app-unisoc-ota/htdocs/luci-static/resources'
view=(resources/'view/system/unisoc-ota.js').read_text(encoding='utf-8')
errors=base64.b64encode((resources/'unisoc-ota/errors.js').read_bytes()).decode()
app=next(p for p in (TOP/'openwrt/luci-app-t551m',TOP/'openwrt/luci-app-mu300') if p.is_dir())
common=(app/'htdocs/luci-static/resources/mu300/common.js').read_text(encoding='utf-8')
with sync_playwright() as engine:
    chrome=Path(args.browser or 'C:/Program Files/Google/Chrome/Application/chrome.exe')
    browser=engine.chromium.launch(headless=True,**({'executable_path':str(chrome)} if chrome.exists() else {}))
    count=0
    for lang in ('zh','en','tr'):
        for dark in (False,True):
            for width in (320,390,1280):
                page=browser.new_page(viewport={'width':width,'height':850});faults=[]
                page.on('pageerror',lambda e:faults.append(str(e)))
                page.route('**/*',lambda route:route.abort())
                page.set_content('<html><body><main id="app"></main><div id="modal"></div></body></html>')
                bg,fg=('#202329','#eee') if dark else ('#fff','#222')
                page.add_style_tag(content=f'html{{--surface:{bg};--text:{fg};--hairline:#888;--brand:#2f7bf6;--text-muted:#888}}body{{margin:16px;background:{bg};color:{fg}}}')
                page.evaluate('''async ([source,common,errors,lang])=>{
                    const base={extend:o=>o};window.L={env:{lang},hasViewPermission:()=>true};
                    const M=new Function('rpc','baseclass','L',common)({declare:()=>()=>Promise.resolve({})},base,L);
                    window.state={identity:{version:'r1'},recovery:{count:0,bytes:0},job:{status:'idle'}};
                    window.calls=[];window.ticks=[];window.notifications=[];
                    window.upload=new Promise(r=>window.uploaded=r);window.check=new Promise(r=>window.checked=r);window.start=new Promise(r=>window.started=r);
                    const rpc={declare:o=>{if(!o.nobatch)throw Error('Batched OTA call');return (...args)=>{calls.push([o.method,args]);if(o.method==='status')return Promise.resolve(state);if(o.method==='inspect')return check;if(o.method==='start')return start;return Promise.resolve({});};}};
                    function E(tag,attrs={},children=[]){const e=document.createElement(tag);for(const [k,v] of Object.entries(attrs)){if(k==='click')e.addEventListener('click',v);else if(k==='hidden'||k==='disabled')e[k]=v;else e.setAttribute(k,v);}for(const child of Array.isArray(children)?children:[children])e.append(child);return e;}
                    const ui={uploadFile:()=>upload,showModal:(title,nodes)=>document.querySelector('#modal').replaceChildren(...nodes),hideModal:()=>document.querySelector('#modal').replaceChildren(),addNotification:(_a,node)=>notifications.push(node.innerText)};
                    window.pageView=new Function('view','rpc','ui','poll','M','E','L',source)(base,rpc,ui,{add:f=>ticks.push(f),remove:f=>ticks.splice(ticks.indexOf(f),1)},M,E,L);
                    const messages=await import('data:text/javascript;base64,'+errors);
                    document.querySelector('#app').append(pageView.render([messages,state]));
                }''',[view,common,errors,lang])
                button=page.locator('.mud-ota-actions button').first
                button.click();assert button.is_disabled();assert button.get_attribute('aria-busy')=='true'
                assert not page.locator('.mud-ota-overlay').is_visible()
                page.evaluate('uploaded()');page.wait_for_selector('.mud-ota-overlay:not([hidden])')
                spinner=page.locator('.mud-ota-spinner')
                assert spinner.evaluate("e=>getComputedStyle(e).animationName")== 'mud-ota-spin'
                page.evaluate("checked({id:'a'.repeat(64),applicable:true,manifest:{base_version:'r1',target_version:'r2',operations:[]}})")
                page.wait_for_function('document.querySelector(".mud-ota-overlay").hidden')
                assert not page.evaluate("calls.some(x=>x[0]==='start')")
                page.locator('#modal .mud-ota-primary').click();assert page.locator('.mud-ota-overlay').is_visible()
                page.evaluate("started({status:'running',patch_id:'a'.repeat(64),phase:'queued'})")
                page.wait_for_function("calls.some(x=>x[0]==='start') && ticks.length>0")
                assert page.evaluate("calls.find(x=>x[0]==='start')[1][0]")=='a'*64
                page.evaluate("state={...state,job:{status:'running',command:'apply',patch_id:'a'.repeat(64),phase:'runtime'}};ticks[0]()")
                assert page.locator('.mud-ota-overlay').is_visible()
                assert page.locator('.mud-ota-overlay .mud-card').evaluate('e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth}')
                if lang!='zh':assert not any('\u4e00'<=c<='\u9fff' for c in page.locator('.mud-ota-overlay').inner_text())
                page.evaluate("state={...state,transaction:{id:'a'.repeat(64),status:'applying',started:4,total:10},job:{...state.job,phase:'applying'}};ticks[0]()")
                page.wait_for_function('document.querySelector(".mud-ota-overlay .mud-note").textContent==="4 / 10"')
                page.evaluate("state={...state,reboot_required:true,transaction:{id:'a'.repeat(64),status:'complete'},job:{status:'complete',command:'apply',result:{id:'a'.repeat(64)}}};ticks[0]()")
                page.wait_for_function('document.querySelector(".mud-ota-overlay").hidden')
                assert not button.is_disabled();assert page.evaluate('notifications.length')==0
                assert not faults,faults
                page.close();count+=1
    browser.close()
print('PASS',count,'OTA upload/check/confirm/prepare/write/complete browser layouts:',TOP.name)
