"""Desktop and mobile free-space workflow, backed only by a synthetic bridge."""
from playwright.sync_api import sync_playwright
from ui_smoke import BRIDGE, fixture_context, settle, layout
from android_ui_smoke import MOBILE


PATCH=r"""
localStorage.setItem('onboarding-v1','seen');
const space={disk_bytes:68719476736,userdata_bytes:34359738368,unallocated_bytes:21474836480,recoverable_bytes:17179869184,
 gaps:[{start_bytes:35433480192,bytes:17179869184,recoverable_bytes:17179869184,reason:''},{start_bytes:55834574848,bytes:4294967296,recoverable_bytes:0,reason:'not_adjacent'}]};
window.__fixture.probed={space};
const expansionInvoke=window.__TAURI_INTERNALS__.invoke;
window.__TAURI_INTERNALS__.invoke=async(command,args={})=>{
 const mobile=command==='plugin:mobile-host|request';
 const op=mobile?args.command:command==='backend_request'?args.command:null;
 const params=mobile?JSON.parse(args.parameters):args.params;
 let value;
 if(op==='plan'&&params.choices.method==='expand'){
  window.__fixture.calls.push({command:op,params});
  if(window.__fixture.forceReset&&params.reset_confirmation!=='yes'){
   value={confirmation_required:'format_android',operation:'expand',reason:'Stock resize unavailable / 原厂扩容不受支持',reset_token:'fixture-token',model:'E5',serial_hint:'1234'};
  }else{
   Object.assign(window.__fixture.tx,{choices:{method:'expand'},status:'ready',stage:'plan',plan:{method:'expand',wipe:!!window.__fixture.forceReset,two_stage:!!window.__fixture.forceReset,openwrt_bytes:0,previous_android_bytes:34359738368,android_bytes:51539607552,reclaimed_bytes:17179869184,target:'mmcblk0p4',space}});
   value=JSON.parse(JSON.stringify(window.__fixture.tx));
  }
 }else if(op==='expand_reset'){
  window.__fixture.calls.push({command:op,params});
  Object.assign(window.__fixture.tx,{status:'ready',stage:'plan',expand_reset_offer:{state:'confirmed'},plan:{...window.__fixture.tx.plan,wipe:true}});
  value=JSON.parse(JSON.stringify(window.__fixture.tx));
 }else{return expansionInvoke(command,args);}
 return mobile?{result:value}:value;
};
window.__expansionEvent=(change)=>{
 Object.assign(window.__fixture.tx,change);
 if(window.__mobileEvent)window.__mobileEvent({type:'transaction',data:JSON.parse(JSON.stringify(window.__fixture.tx))});
 else window.__emitFixture(change);
};
"""


def main():
    checks=0
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for language in ('zh','en'):
            zh=language=='zh'
            for width,mobile in ((1120,False),(360,True),(800,True)):
                for reset in (False,True):
                    options={'viewport':{'width':width,'height':900}}
                    if mobile:options.update(user_agent='Android 15',is_mobile=True,has_touch=True)
                    context=fixture_context(browser,**options)
                    context.add_init_script((MOBILE if mobile else BRIDGE)+PATCH+f"localStorage.setItem('language','{language}');window.__fixture.forceReset={str(reset).lower()};")
                    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                    page.goto('http://127.0.0.1:1420')
                    page.locator('.purpose').filter(has_text='高级选项' if zh else 'Advanced options').click()
                    page.get_by_role('button',name='校验安装包' if zh else 'Verify package',exact=True).click()
                    page.get_by_label('请选择设备' if zh else 'Select a device').select_option('fixture-1234')
                    page.get_by_role('button',name='检查空闲空间并扩容 Android' if zh else 'Inspect free space and expand Android',exact=True).click()
                    scan=page.get_by_role('button',name='扫描未分配空间' if zh else 'Scan unallocated space',exact=True)
                    scan.click();page.locator('.space-gap').first.wait_for()
                    assert page.locator('.space-gap').count()==2
                    assert '16.00 GiB' in page.locator('.space-overview').inner_text()
                    page.get_by_role('button',name='查看扩容计划' if zh else 'Review expansion plan',exact=True).click()
                    if reset:
                        dialog=page.get_by_role('dialog');dialog.wait_for()
                        choose=dialog.get_by_role('button',name='准备清空数据扩容计划' if zh else 'Prepare erase-data expansion plan',exact=True)
                        assert choose.is_disabled()
                        dialog.locator('input').fill('YES');assert choose.is_disabled()
                        dialog.locator('input').fill('yes');choose.click()
                    page.get_by_text('确认 Android 扩容计划' if zh else 'Confirm Android expansion',exact=True).wait_for(state='attached')
                    start=page.get_by_role('button',name='确认扩容' if zh else 'Confirm expansion',exact=True)
                    assert start.is_disabled()
                    assert '32.00 GiB' in page.locator('.plan-list').inner_text()
                    assert '48.00 GiB' in page.locator('.plan-list').inner_text()
                    settle(page)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    if not mobile:layout(page)
                    page.get_by_role('checkbox').check();start.click()
                    page.get_by_role('heading',name='请完成 Android 初始设置' if zh else 'Complete Android initial setup',exact=True).first.wait_for()
                    if not reset:
                        page.evaluate("window.__expansionEvent({stage:'expand_format',status:'waiting_user',expand_committed:true,expand_reset_offer:{state:'awaiting',nonce:'fixture-nonce',reason:'F2FS tool rejected growth'}})")
                        dialog=page.get_by_role('dialog');dialog.wait_for()
                        assert dialog.get_by_role('button',name='准备清空数据扩容计划' if zh else 'Prepare erase-data expansion plan',exact=True).is_disabled()
                        dialog.locator('input').fill('yes')
                        dialog.get_by_role('button',name='准备清空数据扩容计划' if zh else 'Prepare erase-data expansion plan',exact=True).click()
                        page.get_by_text('确认 Android 扩容计划' if zh else 'Confirm Android expansion',exact=True).wait_for(state='attached')
                        assert page.get_by_role('button',name='确认扩容' if zh else 'Confirm expansion',exact=True).is_disabled()
                        assert page.evaluate("window.__fixture.calls.filter(c=>c.command==='expand_reset').length")==1
                    assert not errors,errors
                    checks+=1;context.close()
        browser.close()
    print(f'{checks} expansion flows passed: desktop/phone/tablet, zh/en, scan, preserving plan, typed yes, separate reset confirmation; no hardware access')


if __name__=='__main__':main()
