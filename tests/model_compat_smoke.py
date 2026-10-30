"""Actual GUI model-warning workflow, English/Chinese and small windows."""
from pathlib import Path
from playwright.sync_api import sync_playwright
from ui_smoke import BRIDGE, fixture_context, layout, settle

WARNING = r'''
localStorage.setItem('onboarding-v1','seen');
const originalInvoke=window.__TAURI_INTERNALS__.invoke;
window.__TAURI_INTERNALS__.invoke=async(command,args={})=>{
 if(command==='backend_request' && args.command==='plan' &&
    !(args.params.model_confirmation==='yes' && args.params.model_token==='current-device-package-method')) {
  window.__fixture.calls.push({command:args.command,params:args.params});
  return {confirmation_required:'model_mismatch',model_token:'current-device-package-method',serial_hint:'1234',
   warning:{expected_profile:'e5',expected_models:['E5'],expected_board:null,actual_model:'E5-like hardware',actual_board:'variant'}};
 }
 const result=await originalInvoke(command,args);
 if(command==='backend_request' && args.command==='plan')
  result.model_override={confirmation:'yes',confirmed_at:Date.now()/1000};
 return result;
};
'''


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for language in ('zh','en'):
            context=fixture_context(browser,viewport={'width':800,'height':650})
            context.add_init_script(BRIDGE+WARNING+f"localStorage.setItem('language','{language}');")
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto('http://127.0.0.1:1420');zh=language=='zh'
            page.get_by_role('button',name='安装 OpenWrt' if zh else 'Install OpenWrt',exact=False).click()
            page.get_by_role('button',name='校验安装包' if zh else 'Verify package',exact=True).click()
            page.get_by_label('请选择设备' if zh else 'Select a device').select_option('fixture-1234')
            page.get_by_role('button',name='检查设备' if zh else 'Inspect device',exact=True).click()
            review=page.get_by_role('button',name='查看安装计划' if zh else 'Review installation plan',exact=True)
            review.click();dialog=page.get_by_role('dialog');dialog.wait_for();settle(page)
            dialog.get_by_text('E5-like hardware',exact=False).wait_for()
            proceed=dialog.get_by_role('button',name='仍使用此安装包' if zh else 'Use this package anyway',exact=True)
            self_input=dialog.locator('input')
            for text in ('','YES',' yes','yes '):
                self_input.fill(text);assert proceed.is_disabled()
            self_input.fill('yes');assert proceed.is_enabled()
            dialog.locator('.dialog-footer').get_by_role('button',name='返回选择安装包' if zh else 'Choose another package',exact=True).click()
            assert not [c for c in page.evaluate('window.__fixture.calls') if c['command']=='start']
            review.click();dialog.wait_for();assert self_input.input_value()==''
            self_input.fill('yes');proceed.click();settle(page);layout(page)
            page.get_by_text('已确认使用不匹配机型的安装包' if zh else 'Model exception confirmed for this package',exact=True).wait_for()
            assert page.get_by_role('button',name='确认并开始' if zh else 'Confirm and start',exact=True).is_disabled()
            plans=[c for c in page.evaluate('window.__fixture.calls') if c['command']=='plan']
            assert plans[-1]['params']['model_token']=='current-device-package-method'
            assert plans[-1]['params']['model_confirmation']=='yes'
            assert not errors,errors
            context.close()
        browser.close()
    print('PASS: zh/en model warning, exact yes, cancel/reopen, bounded request and separate final confirmation')


if __name__=='__main__':main()
