"""Boot repair stays within the existing purpose/confirmation workflow."""
from playwright.sync_api import sync_playwright
from ui_smoke import BRIDGE, fixture_context, settle, layout


PATCH = r"""
localStorage.setItem('onboarding-v1','seen');
const originalBootInvoke=window.__TAURI_INTERNALS__.invoke;
window.__TAURI_INTERNALS__.invoke=async(command,args)=>{
 const value=await originalBootInvoke(command,args);
 if(command==='backend_request' && args.command==='plan'){
  value.plan={...value.plan,wipe:false,two_stage:false,offline:false,
   boot_check:{required:true,target:'/dev/block/by-name/boot_b',slot:'b',layout:'part',root:{kernel_release:'7.2.8'},reason:'Boot layout differs / 启动布局不匹配'}};
  window.__fixture.tx.plan=value.plan;
 }
 return value;
};
"""


def main():
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for language,width in (('zh',1120),('en',800)):
            context=fixture_context(browser,viewport={'width':width,'height':850})
            context.add_init_script(BRIDGE+PATCH+f"localStorage.setItem('language','{language}');")
            page=context.new_page();errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto('http://127.0.0.1:1420')
            page.get_by_role('button',name='临时启动已安装的 OpenWrt' if language=='zh' else 'Boot installed OpenWrt once',exact=False).click()
            page.get_by_role('button',name='校验安装包' if language=='zh' else 'Verify package',exact=True).click()
            page.get_by_label('请选择设备' if language=='zh' else 'Select a device').select_option('fixture-1234')
            page.get_by_role('button',name='检查设备' if language=='zh' else 'Inspect device',exact=True).click()
            option=page.get_by_role('checkbox')
            assert not option.is_checked()
            option.check()
            page.get_by_role('button',name='查看安装计划' if language=='zh' else 'Review installation plan',exact=True).click()
            settle(page);layout(page)
            assert page.get_by_text('/dev/block/by-name/boot_b + misc',exact=True).is_visible()
            assert page.get_by_text('此次需要先修复启动镜像' if language=='zh' else 'Boot image repair is needed first',exact=True).is_visible()
            start=page.get_by_role('button',name='确认并开始' if language=='zh' else 'Confirm and start',exact=True)
            assert start.is_disabled()
            page.get_by_role('checkbox').check()
            assert start.is_enabled()
            assert page.evaluate("window.__fixture.calls.find(c=>c.command==='plan').params.choices.repair_boot") is True
            assert page.evaluate("window.__fixture.calls.filter(c=>c.command==='start').length")==0
            assert not errors,errors
            context.close()
        browser.close()
    print('Boot repair UI: same entry, optional rebuild, explicit target/impact and confirmation; Chinese/English passed; no device access')


if __name__=='__main__':main()
