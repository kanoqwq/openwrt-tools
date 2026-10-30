"""Production UI: default strategy and explicit, unconfirmed erase fallback."""
from playwright.sync_api import sync_playwright
from ui_smoke import BRIDGE, fixture_context, layout, settle

ONLINE = r'''
localStorage.setItem('onboarding-v1','seen');
const originalInvoke=window.__TAURI_INTERNALS__.invoke;
window.__TAURI_INTERNALS__.invoke=async(command,args={})=>{
 if(command==='backend_request' && args.command==='online_fallback'){
   window.__fixture.calls.push({command:args.command,params:args.params});
   const tx=window.__fixture.tx;
   return {...tx,stage:'plan',status:'ready',online_fallback:{...tx.online_fallback,state:'confirmed'},plan:{...tx.plan,shrink_strategy:undefined,wipe:true,offline:true}};
 }
 const result=await originalInvoke(command,args);
 if(command==='backend_request' && args.command==='plan'){
   const plan={...result.plan,shrink_strategy:'online-f2fs',wipe:false,offline:false,two_stage:true};
   window.__fixture.tx.plan=plan;result.plan=plan;
 }
 return result;
};
'''


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for language in ('zh','en'):
            context=fixture_context(browser,viewport={'width':800,'height':650})
            context.add_init_script(BRIDGE+ONLINE+f"localStorage.setItem('language','{language}');")
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:1420');zh=language=='zh'
            page.get_by_role('button',name='安装 OpenWrt' if zh else 'Install OpenWrt',exact=False).click()
            select=page.get_by_label('需要缩容时' if zh else 'When shrinking is needed')
            assert select.input_value()=='prefer-online'
            select.select_option('format');assert select.input_value()=='format'
            select.select_option('prefer-online')
            page.get_by_role('button',name='校验安装包' if zh else 'Verify package',exact=True).click()
            page.get_by_label('请选择设备' if zh else 'Select a device').select_option('fixture-1234')
            page.get_by_role('button',name='检查设备' if zh else 'Inspect device',exact=True).click()
            page.get_by_role('button',name='查看安装计划' if zh else 'Review installation plan',exact=True).click()
            settle(page);layout(page)
            page.get_by_text('在线保留 Android 数据缩容' if zh else 'Shrink while preserving Android data',exact=True).wait_for()
            start=page.get_by_role('button',name='确认并开始' if zh else 'Confirm and start',exact=True)
            assert start.is_disabled();page.locator('.confirmation input').check();start.click();settle(page)
            page.evaluate("window.__emitFixture({stage:'online_fallback',status:'waiting_user',online_fallback:{state:'awaiting',nonce:'bound-nonce',reason:'Unsupported kernel / 内核不支持'}})")
            dialog=page.get_by_role('dialog');dialog.wait_for();settle(page)
            action=dialog.get_by_role('button',name='准备清空数据的计划' if zh else 'Prepare erase-data plan',exact=True)
            assert action.is_disabled();dialog.locator('input').fill('YES');assert action.is_disabled()
            dialog.locator('input').fill('yes');assert action.is_enabled()
            # Dismiss does not send an erase command. Reopen the decision.
            dialog.locator('.dialog-footer').get_by_role('button',name='保留当前状态' if zh else 'Keep current state',exact=True).click();settle(page)
            assert not [c for c in page.evaluate('window.__fixture.calls') if c['command']=='online_fallback']
            page.get_by_role('button',name='在线缩容未完成，是否准备清空数据的方案？' if zh else 'Prepare an erase-data plan instead?',exact=True).click()
            assert dialog.locator('input').input_value()==''
            dialog.locator('input').fill('yes')
            dialog.get_by_role('button',name='准备清空数据的计划' if zh else 'Prepare erase-data plan',exact=True).click();settle(page)
            start=page.get_by_role('button',name='确认并开始' if zh else 'Confirm and start',exact=True)
            assert start.is_disabled();assert not page.locator('.confirmation input').is_checked()
            calls=page.evaluate('window.__fixture.calls')
            assert [c for c in calls if c['command']=='online_fallback'][-1]['params']=={'transaction':'a'*32,'nonce':'bound-nonce','confirmation':'yes'}
            assert len([c for c in calls if c['command']=='start'])==1
            assert not errors,errors
            layout(page);context.close()
        browser.close()
    print('PASS: zh/en, small viewport, default online, explicit format, cancel, nonce/yes and new-plan confirmation')


if __name__=='__main__':main()
