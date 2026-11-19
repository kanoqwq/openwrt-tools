"""Actual LuCI view in a browser, with isolated RPC responses and no device I/O."""
import argparse
import base64
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
resources = ROOT/'runtime/luci-app-unisoc-ota/htdocs/luci-static/resources'
source = (resources/'view/system/unisoc-ota.js').read_text(encoding='utf-8')
errors = base64.b64encode((resources/'unisoc-ota/errors.js').read_bytes()).decode()
parser = argparse.ArgumentParser()
parser.add_argument('--browser', type=Path, default=Path('C:/Program Files/Google/Chrome/Application/chrome.exe'))
args = parser.parse_args()

setup = r'''async ([source,errors,initial])=>{
    const missing='RPC call to unisoc-ota/status failed with error -32000: Object not found';
    window.s={missing,notes:[],calls:[],ticks:[],error:null,status:{identity:{version:'r1'},recovery:{count:0},job:{status:'idle'}}};
    function E(tag,attrs={},children=[]){
        const e=document.createElement(tag);
        for(const [key,value] of Object.entries(attrs)){
            if(key==='click')e.addEventListener('click',value);
            else if(key==='hidden'||key==='disabled')e[key]=value;
            else e.setAttribute(key,value);
        }
        for(const child of Array.isArray(children)?children:[children])e.append(child);
        return e;
    }
    const rpc={getSessionID:()=>'fixture-session',declare:({method})=>(...args)=>{
        s.calls.push(method);
        if(method==='status')return s.error?Promise.reject(Error(s.error)):Promise.resolve(s.status);
        if(method==='inspect_begin')return Promise.resolve({status:'complete',ticket:'c'.repeat(32),result:{id:'a'.repeat(64),applicable:true,manifest:{base_version:'r1',target_version:'r2',operations:[]}}});
        return Promise.resolve({status:'running',command:'apply',patch_id:'a'.repeat(64)});
    }};
    const ui={uploadFile:()=>Promise.resolve(),addNotification:(_a,node)=>s.notes.push(node.textContent),
        showModal:(_title,nodes)=>document.querySelector('#modal').replaceChildren(...nodes),
        hideModal:()=>document.querySelector('#modal').replaceChildren(),awaitReconnect:()=>{}};
    const poll={add:f=>s.ticks.push(f),remove:f=>s.ticks=s.ticks.filter(x=>x!==f)};
    const request={post:()=>Promise.resolve({json:()=>({size:1})})};
    const L={env:{cgi_base:'/cgi-bin'},hasViewPermission:()=>true},M={injectCss:()=>{},localizeMenu:()=>{},uiLanguage:()=> 'en'};
    const v=new Function('view','rpc','ui','request','poll','E','L','M',source)({extend:v=>v},rpc,ui,request,poll,E,L,M);
    const messages=await import('data:text/javascript;base64,'+errors);
    document.querySelector('#app').append(v.render([messages,initial?{load_error:Error(missing)}:s.status]));
    window.complete=async ()=>{
        s.error=null;s.status={identity:{version:'r2'},reboot_required:true,recovery:{count:1,available:true},
            job:{status:'complete',command:'apply',result:{id:'a'.repeat(64)}}};
        for(const tick of [...s.ticks])await tick();
    };
}'''

with sync_playwright() as engine:
    browser = engine.chromium.launch(headless=True, **({'executable_path': str(args.browser)} if args.browser.is_file() else {}))
    for initial in (True, False):
        page = browser.new_page()
        page.set_content('<main id="app"></main><div id="modal"></div>')
        page.evaluate(setup, [source, errors, initial])
        if not initial:
            page.get_by_role('button', name='Import patch', exact=True).click()
            page.locator('#ota-full-file').set_input_files(dict(name='update.ota',mimeType='application/octet-stream',buffer=b'fixture'))
            page.locator('#ota-full-file-confirm').click()
            page.get_by_role('button', name='Apply patch', exact=True).click()
            page.wait_for_function('s.ticks.length===1')
        page.evaluate('async ()=>{s.error=s.missing;for(let i=0;i<3;i++)for(const tick of [...s.ticks])await tick();}')
        assert page.evaluate('s.notes.length') == 0
        assert page.get_by_role('button', name='Reboot', exact=True).is_disabled()
        page.evaluate('complete()')
        assert page.locator('.mud-ota-overlay').is_hidden()
        assert page.get_by_role('button', name='Reboot', exact=True).is_enabled()
        assert 'Reboot to use' in page.locator('.mud-ota-activity').inner_text()
        assert page.evaluate('s.notes.length===0 && s.ticks.length===0')
        assert page.evaluate('s.calls.filter(x=>x==="start").length') == (0 if initial else 1)
        page.close()
    browser.close()
print('PASS actual browser: initial registration gap and completed-update reconnect restore reboot controls')
