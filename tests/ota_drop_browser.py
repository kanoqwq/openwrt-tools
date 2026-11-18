"""Actual OTA view: authenticated drag upload, picker, busy and review gates."""
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT/'runtime/luci-app-unisoc-ota/htdocs/luci-static/resources'
SOURCE = (RES/'view/system/unisoc-ota.js').read_text(encoding='utf8')
ERRORS = (RES/'unisoc-ota/errors.js').read_text(encoding='utf8')

FIXTURE = r'''async ([source,errors,lang,writable,running])=>{
 if(window.otaView)window.otaView.unload();document.body.replaceChildren();
 document.documentElement.style.cssText='--surface:#141822;--surface-sunken:#0a0e17;--text:#f9fafb;--text-muted:#909297;--hairline:#f9fafb26;--brand:#00958e;--brand-subtle:#103535';
 document.body.style.cssText='margin:16px;background:#05070e;color:#f9fafb;font-family:system-ui,sans-serif';
 window.calls=[];window.uploads=[];window.pickers=[];window.failUpload=false;
 function E(tag,attrs={},children=[]){const e=document.createElement(tag);for(const [k,v] of Object.entries(attrs)){if(v==null)continue;if(k==='click')e.addEventListener(k,v);else if(k==='hidden'||k==='disabled')e[k]=v;else e.setAttribute(k,v);}for(const c of Array.isArray(children)?children:[children])if(c!=null)e.append(c instanceof Node?c:document.createTextNode(String(c)));return e;}
 const state={identity:{version:'test-r1'},job:{status:running?'running':'idle'},recovery:{count:0,pending:false}};
 const rpc={getSessionID:()=>'fixture-session',declare:o=>(...args)=>{
   calls.push([o.method,args]);
   if(o.method==='status')return Promise.resolve(state);
   if(o.method==='build_status')return Promise.resolve({job:{status:'idle'},baseline:{available:false},uploads:{}});
   if(o.method==='inspect_begin')return Promise.resolve({status:'complete',ticket:'c'.repeat(32),result:{id:'a'.repeat(64),applicable:true,manifest:{base_version:'test-r1',target_version:'test-r2',operations:[]}}});
   return Promise.resolve({});
 }};
 const ui={hideModal:()=>document.querySelector('#modal')?.remove(),showModal:(title,nodes)=>{ui.hideModal();document.body.append(E('div',{id:'modal',role:'dialog'},[E('h3',{},title),...nodes]));},uploadFile:(path,target)=>{pickers.push(path);return new Promise((resolve,reject)=>{window.finishPicker=resolve;window.cancelPicker=reject;});},addNotification:(_a,node)=>{const alert=E('div',{role:'alert'},[node]);document.body.append(alert);return alert;}};
 const request={post:(url,data,options)=>{
   uploads.push({url,path:data.get('filename'),session:data.get('sessionid'),name:data.get('filedata').name,timeout:options.timeout});
   options.progress({loaded:50,total:100});
   return new Promise((resolve,reject)=>{window.finishUpload=()=>resolve({json:()=>failUpload?{failure:2,message:'Fixture upload rejected'}:{size:100}});window.rejectUpload=reject;});
 }};
 const messages=await import('data:text/javascript;base64,'+btoa(unescape(encodeURIComponent(errors))));
 const L={env:{cgi_base:'/custom/cgi'},hasViewPermission:()=>writable};
 const M={injectCss(){const s=E('style',{},'.mud-card{border:1px solid var(--hairline);border-radius:12px}.mud-btn{border:1px solid var(--hairline);border-radius:8px;color:inherit}.mud-btn:disabled{opacity:.5}');document.head.append(s);},localizeMenu(){},uiLanguage:()=>lang};
 window.otaView=new Function('view','rpc','ui','request','poll','E','document','L','M',source)({extend:o=>o},rpc,ui,request,{add(){},remove(){}},E,document,L,M);
 document.body.append(otaView.render([messages,state,{job:{status:'idle'},baseline:{available:false},uploads:{}}]));
 window.drop=(names,event='drop')=>{const dt=new DataTransfer();for(const name of names)dt.items.add(new File(['fixture'],name,{type:'application/octet-stream'}));const e=new DragEvent(event,{bubbles:true,cancelable:true,dataTransfer:dt});document.querySelector('.mud-ota-dropzone').dispatchEvent(e);return e.defaultPrevented;};
}'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',default='C:/Program Files/Google/Chrome/Application/chrome.exe')
    args=parser.parse_args()
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=args.browser,headless=True)
        for lang in ('zh','en','tr'):
            for width in (390,1280):
                page=browser.new_page(viewport={'width':width,'height':1000})
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.route('http://fixture.test/**',lambda r:r.fulfill(body='<body></body>',content_type='text/html'))
                page.goto('http://fixture.test/')
                def setup(write=True,running=False):page.evaluate(FIXTURE,[SOURCE,ERRORS,lang,write,running])
                setup();zone=page.locator('.mud-ota-dropzone')
                assert not zone.is_disabled()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if lang!='zh':assert not zone.evaluate('e=>/[\u3400-\u9fff]/.test(e.innerText)')
                # Invalid/multiple drops never upload or start inspection; their
                # notifications replace the previous input error.
                for names in ([],['one.ota','two.zip'],['wrong.txt']):
                    assert page.evaluate('drop',names)
                    assert page.evaluate('uploads.length')==0
                    assert page.locator('[role="alert"]').count()==1
                    assert not page.evaluate("calls.some(x=>x[0]==='inspect_begin')")
                for name in ('update.ota','firmware-update.ZIP','<img src=x onerror=alert(1)>.zip'):
                    setup();zone=page.locator('.mud-ota-dropzone')
                    page.evaluate("drop(['update.ota'],'dragover')")
                    assert 'is-dragging' in zone.get_attribute('class')
                    page.evaluate('drop',[name])
                    assert zone.is_disabled()
                    assert '50%' in zone.inner_text()
                    assert zone.locator('img').count()==0
                    sent=page.evaluate('uploads[0]')
                    assert sent==dict(url='/custom/cgi/cgi-upload',path='/etc/unisoc-ota/upload.ota',session='fixture-session',name=name,timeout=0)
                    page.evaluate("drop(['second.ota'])")
                    assert page.evaluate('uploads.length')==1
                    assert not page.evaluate("calls.some(x=>x[0]==='inspect_begin')")
                    page.evaluate('finishUpload()')
                    page.wait_for_selector('#modal')
                    assert page.evaluate("calls.filter(x=>x[0]==='inspect_begin').length")==1
                    assert not page.evaluate("calls.some(x=>x[0]==='start'||x[0]==='start_generated')")
                    assert not zone.is_disabled()
                # Native click and keyboard retain LuCI's existing upload UI.
                setup();zone=page.locator('.mud-ota-dropzone');zone.focus();page.keyboard.press('Enter')
                assert page.evaluate('pickers')==['/etc/unisoc-ota/upload.ota']
                page.evaluate('finishPicker()');page.wait_for_selector('#modal')
                assert not page.evaluate("calls.some(x=>x[0]==='start')")
                setup();page.locator('.mud-ota-dropzone').click()
                page.evaluate("cancelPicker(new Error('Upload has been cancelled'))")
                page.wait_for_function('!document.querySelector(".mud-ota-dropzone").disabled')
                assert page.locator('[role="alert"]').count()==0
                for write,running in ((False,False),(True,True)):
                    setup(write,running);page.evaluate("drop(['update.ota'])")
                    assert page.evaluate('uploads.length')==0
                    assert page.locator('.mud-ota-dropzone').is_disabled()
                setup();page.evaluate("failUpload=true;drop(['update.zip']);finishUpload()")
                page.wait_for_function('!document.querySelector(".mud-ota-dropzone").disabled')
                assert not page.evaluate("calls.some(x=>x[0]==='inspect_begin')")
                assert page.locator('[role="alert"]').count()==1
                setup();page.evaluate("drop(['update.ota']);otaView.unload();finishUpload()")
                page.wait_for_timeout(25)
                assert not page.evaluate("calls.some(x=>x[0]==='inspect_begin')")
                assert not errors,errors
                page.close()
        browser.close()
    print('PASS OTA drop/picker: 3 languages, mobile/desktop, authenticated fixed target, progress, ZIP/OTA, validation, permission/busy/unload gates, failures and explicit review before apply')


if __name__=='__main__':main()
