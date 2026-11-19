"""Actual OTA view: authenticated drag upload, picker, busy and review gates."""
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT/'runtime/luci-app-unisoc-ota/htdocs/luci-static/resources'
SOURCE = (RES/'view/system/unisoc-ota.js').read_text(encoding='utf8')
ERRORS = (RES/'unisoc-ota/errors.js').read_text(encoding='utf8')

FIXTURE = r'''async ([source,errors,lang,writable,running,baselineReady])=>{
 if(window.otaView)window.otaView.unload();document.body.replaceChildren();
 document.documentElement.style.cssText='--surface:#141822;--surface-sunken:#0a0e17;--text:#f9fafb;--text-muted:#909297;--hairline:#f9fafb26;--brand:#00958e;--brand-subtle:#103535';
 document.body.style.cssText='margin:16px;background:#05070e;color:#f9fafb;font-family:system-ui,sans-serif';
 window.calls=[];window.uploads=[];window.pickers=[];window.failUpload=false;window.buildStatusError=false;window.blockPreflight=false;
 window.builder={job:{status:'idle'},baseline:{available:!!baselineReady},uploads:{}};
 function E(tag,attrs={},children=[]){const e=document.createElement(tag);for(const [k,v] of Object.entries(attrs)){if(v==null)continue;if(k==='click')e.addEventListener(k,v);else if(k==='hidden'||k==='disabled')e[k]=v;else e.setAttribute(k,v);}for(const c of Array.isArray(children)?children:[children])if(c!=null)e.append(c instanceof Node?c:document.createTextNode(String(c)));return e;}
 const state={identity:{version:'test-r1'},job:{status:running?'running':'idle'},recovery:{count:0,pending:false}};
 const rpc={getSessionID:()=>'fixture-session',declare:o=>(...args)=>{
   calls.push([o.method,args]);
   if(o.method==='status')return Promise.resolve(state);
   if(o.method==='build_status'){if(buildStatusError)return Promise.reject(Error('Fixture directory preparation failed'));if(blockPreflight)return new Promise(r=>window.releasePreflight=()=>r(builder));return Promise.resolve(builder);}
   if(o.method==='inspect_begin')return Promise.resolve({status:'complete',ticket:'c'.repeat(32),result:{id:'a'.repeat(64),applicable:true,manifest:{base_version:'test-r1',target_version:'test-r2',operations:[]}}});
   return Promise.resolve({});
 }};
 const ui={hideModal:()=>document.querySelector('#modal')?.remove(),showModal:(title,nodes)=>{ui.hideModal();document.body.append(E('div',{id:'modal',role:'dialog'},[E('h3',{},title),...nodes]));},uploadFile:(path,target)=>{pickers.push(path);return new Promise((resolve,reject)=>{window.finishPicker=resolve;window.cancelPicker=reject;});},addNotification:(_a,node)=>{const alert=E('div',{role:'alert'},[node]);document.body.append(alert);return alert;}};
 const request={post:(url,data,options)=>{
   uploads.push({url,path:data.get('filename'),session:data.get('sessionid'),name:data.get('filedata').name,timeout:options.timeout});
   options.progress({loaded:50,total:100});
   return new Promise((resolve,reject)=>{window.finishUpload=(reply)=>{if(!failUpload&&!reply?.failure&&data.get('filename').includes('/builder/'))builder.uploads[data.get('filename').split('/').at(-1)]=100;resolve({json:()=>reply||(failUpload?{failure:2,message:'Fixture upload rejected'}:{size:100})});};window.rejectUpload=reject;});
 }};
 const messages=await import('data:text/javascript;base64,'+btoa(unescape(encodeURIComponent(errors))));
 const L={env:{cgi_base:'/custom/cgi'},hasViewPermission:()=>writable};
 const M={injectCss(){const s=E('style',{},'.mud-card{border:1px solid var(--hairline);border-radius:12px}.mud-btn{border:1px solid var(--hairline);border-radius:8px;color:inherit}.mud-btn:disabled{opacity:.5}');document.head.append(s);},localizeMenu(){},uiLanguage:()=>lang};
 window.otaView=new Function('view','rpc','ui','request','poll','E','document','L','M',source)({extend:o=>o},rpc,ui,request,{add(){},remove(){}},E,document,L,M);
 document.body.append(otaView.render([messages,state,builder]));
 window.drop=(names,event='drop',id='ota-patch-upload')=>{const dt=new DataTransfer();for(const name of names)dt.items.add(new File(['fixture'],name,{type:'application/octet-stream'}));const e=new DragEvent(event,{bubbles:true,cancelable:true,dataTransfer:dt});document.querySelector('#'+id).dispatchEvent(e);return e.defaultPrevented;};
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
                def setup(write=True,running=False,baseline=False):page.evaluate(FIXTURE,[SOURCE,ERRORS,lang,write,running,baseline])
                setup();zone=page.locator('#ota-patch-upload')
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
                    setup();zone=page.locator('#ota-patch-upload')
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
                # Click/keyboard select first, then prepare and upload the fixed patch slot.
                setup();zone=page.locator('#ota-patch-upload');zone.focus();page.keyboard.press('Enter')
                page.wait_for_selector('#ota-full-file')
                assert page.evaluate('uploads.length')==0
                page.locator('#ota-full-file').set_input_files(dict(name='patch.ota',mimeType='application/octet-stream',buffer=b'fixture'))
                page.locator('#ota-full-file-confirm').click()
                page.wait_for_function('uploads.length===1')
                assert page.evaluate("calls.some(x=>x[0]==='status')")
                page.evaluate('finishUpload()');page.wait_for_selector('#modal')
                assert not page.evaluate("calls.some(x=>x[0]==='start')")
                setup();page.locator('#ota-patch-upload').click()
                page.locator('#modal button').first.click()
                page.wait_for_function('!document.querySelector("#ota-patch-upload").disabled')
                assert page.locator('[role="alert"]').count()==0
                for write,running in ((False,False),(True,True)):
                    setup(write,running);page.evaluate("drop(['update.ota'])")
                    assert page.evaluate('uploads.length')==0
                    assert page.locator('#ota-patch-upload').is_disabled()
                setup();page.evaluate("failUpload=true;drop(['update.zip'])")
                page.wait_for_function('uploads.length===1');page.evaluate('finishUpload()')
                page.wait_for_function('!document.querySelector("#ota-patch-upload").disabled')
                assert not page.evaluate("calls.some(x=>x[0]==='inspect_begin')")
                assert page.locator('[role="alert"]').count()==1
                setup();page.evaluate("drop(['update.ota'])")
                page.wait_for_function('uploads.length===1');page.evaluate('otaView.unload();finishUpload()')
                page.wait_for_timeout(25)
                assert not page.evaluate("calls.some(x=>x[0]==='inspect_begin')")
                # Current-version initialization and the new-version slot use
                # fixed old.zip/new.zip names regardless of the dropped name.
                setup()
                assert page.locator('#ota-baseline-setup').is_visible()
                assert page.locator('#ota-old-release').is_hidden()
                for id,name in [('ota-current-upload','old.zip'),('ota-new-upload','new.zip')]:
                    before=page.evaluate('uploads.length')
                    page.evaluate("id=>drop(['wrong.ota'],'drop',id)",id)
                    assert page.evaluate('uploads.length')==before
                    page.evaluate("id=>drop(['one.zip','two.zip'],'drop',id)",id)
                    assert page.evaluate('uploads.length')==before
                    page.evaluate("id=>drop(['renamed-full-firmware.ZIP'],'drop',id)",id)
                    page.wait_for_function('n=>uploads.length===n',arg=before+1)
                    sent=page.evaluate('uploads.at(-1)')
                    assert sent['path']=='/etc/unisoc-ota/builder/'+name
                    assert sent['name']=='renamed-full-firmware.ZIP'
                    assert page.evaluate("calls.some(x=>x[0]==='build_status')"),'prepare destination before CGI upload'
                    assert '50%' in page.locator('#'+id).inner_text()
                    assert page.locator('#ota-patch-upload').is_disabled()
                    page.evaluate("drop(['duplicate.zip'],'drop','ota-new-upload')")
                    assert page.evaluate('uploads.length')==before+1
                    page.evaluate('finishUpload()')
                    page.wait_for_function('id=>!document.getElementById(id).disabled',arg=id)
                    assert not page.evaluate("calls.some(x=>['generate','start','start_generated','inspect_begin'].includes(x[0]))")
                # Pair mode exposes two independent targets. Completing both
                # enables generation, but does not start it automatically.
                setup();page.select_option('#ota-build-mode','pair')
                assert page.locator('#ota-baseline-setup').is_hidden()
                assert page.locator('#ota-old-release').is_visible()
                page.evaluate("drop(['hidden.zip'],'drop','ota-current-upload')")
                assert page.evaluate('uploads.length')==0
                for id,name in [('ota-old-upload','old.zip'),('ota-new-upload','new.zip')]:
                    before=page.evaluate('uploads.length')
                    page.evaluate("id=>drop(['full.zip'],'drop',id)",id)
                    page.wait_for_function('n=>uploads.length===n',arg=before+1)
                    assert page.evaluate('uploads.at(-1).path')=='/etc/unisoc-ota/builder/'+name
                    page.evaluate('finishUpload()')
                    page.wait_for_function('id=>!document.getElementById(id).disabled',arg=id)
                assert not page.evaluate("calls.some(x=>x[0]==='generate')")
                assert page.evaluate("builder.uploads['old.zip']===100&&builder.uploads['new.zip']===100")
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                # A valid current baseline hides initialization; a dropped ZIP
                # still cannot overwrite that hidden current-version slot.
                setup(baseline=True)
                assert page.locator('#ota-baseline-setup').is_hidden()
                page.evaluate("drop(['hidden.zip'],'drop','ota-current-upload')")
                assert page.evaluate('uploads.length')==0
                # Recheck server busy/error before writing any full ZIP.
                for condition in ('buildStatusError=true',"builder.job={status:'running'}"):
                    setup();page.evaluate(condition+";drop(['new.zip'],'drop','ota-new-upload')")
                    page.wait_for_function("calls.some(x=>x[0]==='build_status')")
                    page.wait_for_timeout(25)
                    assert page.evaluate('uploads.length')==0
                    assert not page.evaluate("calls.some(x=>x[0]==='generate')")
                setup();page.evaluate("blockPreflight=true;drop(['new.zip'],'drop','ota-new-upload')")
                page.wait_for_function('!!window.releasePreflight')
                page.evaluate('otaView.unload();releasePreflight()');page.wait_for_timeout(25)
                assert page.evaluate('uploads.length')==0
                # Click-to-upload still targets the corresponding builder slot.
                setup();page.locator('#ota-current-upload').click()
                page.wait_for_selector('#ota-full-file')
                assert not page.evaluate("calls.some(x=>x[0]==='build_status')"),'preparation must wait for selection'
                page.locator('#ota-full-file').set_input_files(dict(name='baseline.zip',mimeType='application/zip',buffer=b'PK fixture'))
                page.locator('#ota-full-file-confirm').click()
                page.wait_for_function('uploads.length===1')
                assert page.evaluate('uploads[0].path')=='/etc/unisoc-ota/builder/old.zip'
                page.evaluate('finishUpload()')
                page.wait_for_function('!document.querySelector("#ota-current-upload").disabled')
                # Cancel and unload release the picker without uploading/errors.
                setup();page.locator('#ota-current-upload').click()
                page.wait_for_selector('#ota-full-file');page.locator('#modal button').first.click()
                page.wait_for_function('!document.querySelector("#ota-current-upload").disabled')
                assert page.evaluate('uploads.length')==0
                assert page.locator('[role="alert"]').count()==0
                setup();page.locator('#ota-current-upload').click()
                page.wait_for_selector('#ota-full-file');page.evaluate('otaView.unload()')
                page.wait_for_timeout(25);assert page.evaluate('uploads.length')==0
                assert page.locator('#modal').count()==0
                # A job starting during file selection still blocks the upload.
                setup();page.locator('#ota-current-upload').click()
                page.locator('#ota-full-file').set_input_files(dict(name='baseline.zip',mimeType='application/zip',buffer=b'PK fixture'))
                page.evaluate("builder.job={status:'running'}")
                page.locator('#ota-full-file-confirm').click();page.wait_for_timeout(25)
                assert page.evaluate('uploads.length')==0
                assert not page.evaluate("calls.some(x=>['generate','start','start_generated'].includes(x[0]))")
                # Parent lost after transfer started: exactly one reprepare/retry.
                setup();page.evaluate("drop(['full.zip'],'drop','ota-current-upload')")
                page.wait_for_function('uploads.length===1')
                page.evaluate("finishUpload({failure:2,message:'Failed to open target file'})")
                page.wait_for_function('uploads.length===2')
                assert page.evaluate("calls.filter(x=>x[0]==='build_status').length")>=2
                page.evaluate('finishUpload()')
                page.wait_for_function('!document.querySelector("#ota-current-upload").disabled')
                assert page.locator('[role="alert"]').count()==0
                # The patch slot also prepares again after a final-target ENOENT.
                setup();page.evaluate("drop(['update.ota'])")
                page.wait_for_function('uploads.length===1')
                page.evaluate("finishUpload({failure:2,message:'Failed to open target file'})")
                page.wait_for_function('uploads.length===2')
                assert page.evaluate("calls.filter(x=>x[0]==='status').length")==2
                page.evaluate('finishUpload()');page.wait_for_selector('#modal')
                assert page.evaluate("calls.filter(x=>x[0]==='inspect_begin').length")==1
                assert not page.evaluate("calls.some(x=>['start','start_generated'].includes(x[0]))")
                # Other errno values and a repeated ENOENT are never retried forever.
                for code,attempts in ((28,1),(30,1),(13,1),(2,2)):
                    setup();page.evaluate("drop(['full.zip'],'drop','ota-current-upload')")
                    for attempt in range(attempts):
                        page.wait_for_function('n=>uploads.length===n',arg=attempt+1)
                        page.evaluate("code=>finishUpload({failure:code,message:'Failed to open target file'})",code)
                    page.wait_for_function('!document.querySelector("#ota-current-upload").disabled')
                    assert page.evaluate('uploads.length')==attempts
                    alert=page.locator('[role="alert"]');assert alert.count()==1
                    assert 'errno='+str(code) in alert.text_content()
                    assert '/etc/unisoc-ota/builder/old.zip' in alert.text_content()
                    assert not page.evaluate("calls.some(x=>['generate','start','start_generated'].includes(x[0]))")
                assert not errors,errors
                page.close()
        browser.close()
    print('PASS OTA patch/builder drop and picker: 3 languages, mobile/desktop, fixed destinations, progress, validation, baseline and pair modes, directory preflight, busy/unload gates and no automatic generation/apply')


if __name__=='__main__':main()
