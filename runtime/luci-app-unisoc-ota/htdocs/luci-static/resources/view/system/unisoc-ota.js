'use strict';
'require view';
'require rpc';
'require ui';
'require poll';
'require mu300.common as M';

var callStatus = rpc.declare({object:'unisoc-ota', method:'status', nobatch:true});
var callInspect = rpc.declare({object:'unisoc-ota', method:'inspect', nobatch:true});
var callStart = rpc.declare({object:'unisoc-ota', method:'start', params:['id'], nobatch:true});
var callRollback = rpc.declare({object:'unisoc-ota', method:'rollback', nobatch:true});
var callRollbackCheck = rpc.declare({object:'unisoc-ota', method:'rollback_check', nobatch:true});
var callDiscard = rpc.declare({object:'unisoc-ota', method:'discard_recovery', nobatch:true});
var callReboot = rpc.declare({object:'unisoc-ota', method:'reboot', nobatch:true});
var callBuildStatus = rpc.declare({object:'unisoc-ota', method:'build_status', nobatch:true});
var callGenerate = rpc.declare({object:'unisoc-ota', method:'generate', params:['mode'], nobatch:true});
var callInspectGenerated = rpc.declare({object:'unisoc-ota', method:'inspect_generated', nobatch:true});
var callStartGenerated = rpc.declare({object:'unisoc-ota', method:'start_generated', params:['id'], nobatch:true});

var CSS = `
.mud-ota{max-width:1200px;margin:0 auto;padding:4px 0 16px}
.mud-ota .mud-ota-heading{margin:0 0 22px}.mud-ota .mud-ota-heading h2{border:0;padding:0;margin:5px 0 9px;font-size:1.5rem;letter-spacing:-.025em;color:var(--text,#222)}
.mud-ota .mud-note{margin:0;line-height:1.7;color:var(--text-muted,var(--text-light,#777));font-size:.8rem}
.mud-ota-kicker{font-size:.7rem;font-weight:650;letter-spacing:.08em;color:var(--brand,var(--primary,#2f7bf6))}
.mud-ota .mud-card{padding:22px;min-width:0}.mud-ota .mud-card:hover{border-color:var(--hairline,var(--border,#e3e6ea))}
.mud-ota-hero{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:18px}
.mud-ota-version{min-width:0;flex:1}.mud-ota-version code{display:block;margin-top:9px;padding:0;border:0;background:none;color:var(--text,#222);font-size:.85rem;line-height:1.6;overflow-wrap:anywhere}
.mud-ota-status{display:inline-flex;align-items:center;gap:7px;font-size:.76rem;font-weight:600;color:var(--success,#25985a)}
.mud-ota-status::before{content:'';width:7px;height:7px;flex:none;border-radius:50%;background:currentColor}
.mud-ota-status[data-state=pending]{color:var(--warning,#ad7400)}.mud-ota-status[data-state=running]{color:var(--brand,var(--primary,#2f7bf6))}
.mud-ota-grid{display:grid;grid-template-columns:1.1fr 1fr;gap:18px;margin-bottom:18px}
.mud-ota-card-head{display:flex;gap:12px;align-items:center;margin-bottom:18px}.mud-ota .mud-ota-card-head h3{margin:0;padding:0;border:0;font-size:1rem;color:var(--text,#222)}
.mud-ota-icon{display:inline-flex;flex:none;align-items:center;justify-content:center;width:38px;height:38px;border-radius:var(--radius-base,.5rem);color:var(--brand,var(--primary,#2f7bf6));background:var(--brand-subtle,var(--background-alt,#f4f6f8))}
.mud-ota-icon svg{width:21px;height:21px;display:block}.mud-ota .mud-btn .mud-ota-icon{width:17px;height:17px;background:none;color:inherit}.mud-ota .mud-btn .mud-ota-icon svg{width:17px;height:17px}
.mud-ota-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:20px}.mud-ota .mud-btn{min-height:38px;padding:8px 14px;line-height:1.4}
.mud-ota .mud-ota-primary,.mud-ota-modal .mud-ota-primary{background:var(--brand,var(--primary,#2f7bf6));border-color:var(--brand,var(--primary,#2f7bf6));color:var(--on-brand,#fff)}
.mud-ota .mud-ota-footnote{padding-top:16px;margin-top:18px;border-top:1px solid var(--hairline,var(--border,#e3e6ea))}
.mud-ota-space{display:flex;align-items:baseline;gap:12px;margin:3px 0 10px;flex-wrap:wrap}.mud-ota-space strong{font-size:1.65rem;font-weight:650;letter-spacing:-.03em;color:var(--text,#222)}
.mud-ota-size-note{font-size:.77rem;color:var(--text-muted,#777)}.mud-ota-activity{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.7;color:var(--text,#222);font-size:.84rem}
.mud-ota-overlay{position:fixed;inset:0;z-index:20000;display:grid;place-items:center;padding:24px;background:rgba(0,0,0,.36)}
.mud-ota-overlay[hidden]{display:none}.mud-ota-overlay .mud-card{width:min(100%,440px);box-sizing:border-box;display:flex;flex-direction:column;align-items:center;gap:16px;text-align:center;background:var(--surface,var(--background-color-high,#fff));color:var(--text,var(--text-color-high,#222))}
.mud-ota-spinner{display:inline-block;width:30px;height:30px;box-sizing:border-box;border:3px solid var(--hairline,#ddd);border-top-color:var(--brand,var(--primary,#2f7bf6));border-radius:50%;animation:mud-ota-spin .8s linear infinite}
.mud-ota .mud-btn[aria-busy=true]::after{content:'';width:13px;height:13px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:mud-ota-spin .8s linear infinite}
@keyframes mud-ota-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.mud-ota-spinner,.mud-ota .mud-btn[aria-busy=true]::after{animation-duration:2s}}
.mud-ota-modal-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;margin-top:22px}.mud-ota-error pre{max-height:180px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.mud-ota-builder{margin-bottom:18px}.mud-ota-build-fields{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:16px 0}.mud-ota-build-fields select{max-width:100%;min-height:38px}.mud-ota-build-fields label{font-size:.85rem}.mud-ota-build-upload{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.mud-ota-build-upload code{overflow-wrap:anywhere;font-size:.8rem}.mud-ota-build-status{margin-top:16px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:.84rem}.mud-ota-build-baseline{margin:10px 0;line-height:1.6}
@media(max-width:720px){.mud-ota-grid{grid-template-columns:1fr}.mud-ota .mud-card{padding:18px}.mud-ota-hero{align-items:flex-start;flex-wrap:wrap}.mud-ota-hero>.mud-btn{margin-top:0}.mud-ota .mud-ota-heading h2{font-size:1.3rem}}
`;

function icon(kind) {
    var paths={upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
        recovery:'<path d="M3 11a9 9 0 1 1 2.5 7M3 4v7h7M12 7v5l3 2"/>',
        power:'<path d="M12 3v8M6 5a9 9 0 1 0 12 0"/>',
        history:'<path d="M4 6h16M4 12h10M4 18h7"/>',
        trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>'};
    var node=E('span', {class:'mud-ota-icon', 'aria-hidden':'true'});
    node.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+paths[kind]+'</svg>';
    return node;
}

return view.extend({
    load: function() {
        return Promise.all([import(L.resource('unisoc-ota/errors.js')),
            callStatus().catch(function(e) { return {load_error:e}; }),
            callBuildStatus().catch(function(e) { return {load_error:e}; })]);
    },
    render: function(loaded) {
        M.injectCss(); M.localizeMenu();
        var messages=loaded[0], state=loaded[1], lang=M.uiLanguage();
        var zh=lang==='zh', tr=function(a,b,c){return zh?a:lang==='tr'?(c||b):b;};
        var error=function(r){if(r&&r.error){var e=new Error(r.error);e.otaBackend=true;throw e;}return r;};
        var detail=E('div',{class:'mud-ota-activity',role:'status','aria-live':'polite'});
        var version=E('code'), badge=E('span',{class:'mud-ota-status'});
        var size=E('strong',{},'--'), recoveryNote=E('p',{class:'mud-note'});
        function action(label,kind,cls){return E('button',{type:'button',class:'mud-btn '+(cls||''),'aria-label':label,disabled:!L.hasViewPermission()},[icon(kind),label]);}
        var button=action(tr('导入差异包','Import patch','Yama içe aktar'),'upload','mud-ota-primary');
        var rollback=action(tr('撤销上次补丁','Roll back last patch','Son yamayı geri al'),'recovery');
        var discard=action(tr('删除恢复副本','Delete recovery copy','Kurtarma kopyasını sil'),'trash','warn');
        var reboot=action(tr('重启设备','Reboot','Yeniden başlat'),'power');
        var buildState=loaded[2]||{job:{status:'idle'},baseline:{available:false}}, buildBusy=false, buildTick=null, shownBuild=null;
        var busy=false, watching=false, pollError=false, rebooting=false, disposed=false, pollTick, last=state, localPhase=null, pendingAction=null, pendingError=null;
        var initialRetries=state.load_error?15:0;
        var workingTitle=E('strong'), workingDetail=E('p',{class:'mud-note'});
        var overlay=E('div',{class:'mud-ota-overlay',hidden:true,role:'status','aria-live':'polite'},[
            E('div',{class:'mud-card'},[E('span',{class:'mud-ota-spinner','aria-hidden':'true'}),workingTitle,workingDetail])]);
        function phaseTitle(value){return ({
            upload:tr('请选择并上传补丁…','Select and upload a patch…','Yamayı seçip yükleyin…'),
            checking:tr('正在检查补丁…','Checking patch…','Yama kontrol ediliyor…'),
            staging:tr('正在准备更新文件…','Preparing update files…','Güncelleme dosyaları hazırlanıyor…'),
            runtime:tr('正在准备更新…','Preparing update…','Güncelleme hazırlanıyor…'),
            applying:tr('正在更新系统…','Updating system…','Sistem güncelleniyor…'),
            reconnecting:tr('正在确认更新状态…','Checking update status…','Güncelleme durumu kontrol ediliyor…'),
            recovery:tr('正在检查恢复副本…','Checking recovery copy…','Kurtarma kopyası kontrol ediliyor…'),
            rollback:tr('正在恢复系统…','Restoring system…','Sistem geri yükleniyor…'),
            discard:tr('正在删除恢复副本…','Deleting recovery copy…','Kurtarma kopyası siliniyor…'),
            reboot:tr('正在重启设备…','Rebooting device…','Cihaz yeniden başlatılıyor…')
        })[value]||tr('正在准备更新…','Preparing update…','Güncelleme hazırlanıyor…');}
        function progress(r){
            var job=r.job||{}, transaction=r.transaction||{}, value=localPhase||job.phase||'applying';
            overlay.hidden=!(localPhase&&localPhase!=='upload'||job.status==='running'||(r.recovery||{}).pending);
            workingTitle.textContent=phaseTitle(value);
            var writing=!localPhase&&transaction.id===job.patch_id&&transaction.total>0&&transaction.status==='applying';
            workingDetail.textContent=writing?transaction.started+' / '+transaction.total:
                localPhase==='checking'?tr('检查完成后会展示更新内容，请确认后再应用。','Review the changes after checks finish, then confirm to apply.','Kontrol bitince değişiklikleri inceleyip uygulamayı onaylayın.'):
                tr('正在处理，请稍候。更新开始后关闭网页不会中断任务。','Please wait. Once started, the update continues even if you close this page.','Lütfen bekleyin. Başladıktan sonra sayfa kapatılsa da güncelleme sürer.');
        }
        function phase(value){if(disposed)return;localPhase=value;if(!value){show(last);return;}busy=true;progress(last);controls();detail.textContent=phaseTitle(value);badge.setAttribute('data-state','running');badge.textContent=phaseTitle(value);}
        function errorNode(e){var info=messages.explainError(e,!zh);if(/Incomplete patch inspection response/.test(info.details))info.message=tr('补丁检查结果不完整，未开始更新。请更新插件后重新检查。','Patch inspection is incomplete. No update started. Update the plugin and inspect again.','Yama kontrolü eksik. Güncelleme başlamadı. Eklentiyi güncelleyip yeniden kontrol edin.');if(/Inspect this patch|Upload differs/.test(info.details))info.message=tr('补丁检查结果已失效或文件已变化，未开始写入。请重新导入并检查。','The review is stale or the file changed. No writes started. Import and inspect it again.','Kontrol geçersiz veya dosya değişti. Yazma başlamadı. Yeniden içe aktarıp kontrol edin.');if(/There is no item named ['"]manifest\.json['"]/.test(info.details))info.message=tr('请选择有效的 update.ota 或包含它的更新 ZIP；完整固件请使用下方的差分生成功能。','Select a valid update.ota or an update ZIP containing it. Use the generator below for full firmware ZIPs.','Geçerli update.ota veya onu içeren güncelleme ZIP dosyasını seçin.');if(/ZIP contains no OTA/.test(info.details))info.message=tr('此 ZIP 不包含 .ota；完整固件请使用下方的差分生成功能。','This ZIP has no .ota. For full firmware, use the generator below.','Bu ZIP .ota içermiyor. Tam sürüm için aşağıdaki oluşturucuyu kullanın.');if(/multiple OTA/.test(info.details))info.message=tr('ZIP 中有多个 .ota，请单独选择需要的补丁。','This ZIP contains multiple .ota files. Select the desired patch directly.','ZIP birden fazla .ota içeriyor. İstenen yamayı doğrudan seçin.');if(/Recovery copy was deleted/.test(info.details))info.message=tr('恢复副本已删除，无法再撤销这次补丁。当前系统不受影响。','The recovery copy was deleted. This patch can no longer be undone; the current system is unaffected.','Kurtarma kopyası silindi. Son yama artık geri alınamaz.');return E('div',{class:'mud-ota-error'},[
            E('p',{},info.message),E('details',{},[E('summary',{},tr('技术详情','Technical details','Teknik ayrıntılar')),E('pre',{},info.details)])]);}
        function failed(e){if(!disposed)ui.addNotification(null,errorNode(e),'error');}
        function controls(){
            var info=last.recovery||{}, writable=!busy&&!buildBusy&&!rebooting&&!info.pending&&L.hasViewPermission();
            button.disabled=!writable;
            rollback.disabled=!writable||!(info.available===true||(!last.recovery&&last.transaction&&last.transaction.status==='complete'));
            discard.disabled=!writable||!(info.count>0);
            reboot.disabled=!writable;
            button.setAttribute('aria-busy',localPhase==='upload'||localPhase==='checking'?'true':'false');
            buildControls();
        }
        function bytes(value){if(value==null)return '--';if(value<1024)return value+' B';var i=Math.min(3,Math.floor(Math.log(value)/Math.log(1024)));return (value/Math.pow(1024,i)).toFixed(1)+' '+['B','KiB','MiB','GiB'][i];}
        function show(r){
            if(disposed||rebooting)return;
            error(r); last=r; version.textContent=(r.identity||{}).version||'--';
            var job=r.job||{}, info=r.recovery||{};
            if(localPhase==='reconnecting')localPhase=null;
            busy=!!localPhase||job.status==='running'||info.pending===true;
            size.textContent=bytes(info.bytes);
            recoveryNote.textContent=info.available?tr('恢复副本可用于撤销上次补丁。确认更新稳定后，可删除副本释放空间。','Use this copy to undo the last patch. Delete it to reclaim space once the update is verified.','Son yamayı geri almak için bu kopyayı kullanın. Güncelleme doğrulandıktan sonra yer açmak için silebilirsiniz.'):
                info.deleted?tr('恢复副本已删除，当前系统保留。无法再撤销上次补丁。','Recovery copy deleted. The current system is retained; the last patch can no longer be undone.','Kurtarma kopyası silindi. Mevcut sistem korunur; son yama artık geri alınamaz.'):
                tr('暂无可用的恢复副本。成功应用补丁时会自动创建。','No recovery copy available. One is created automatically when a patch is applied.','Kurtarma kopyası yok. Yama uygulanırken otomatik oluşturulur.');
            badge.setAttribute('data-state',busy?'running':r.reboot_required===true?'pending':'ready');
            badge.textContent=busy?tr('正在更新','Updating','Güncelleniyor'):r.reboot_required===true?tr('待重启','Reboot required','Yeniden başlatma gerekli'):tr('系统就绪','System ready','Sistem hazır');
            if(busy){detail.textContent=phaseTitle(localPhase||job.phase||'applying');if(job.status==='running'||info.pending)watch();}
            else if(job.status==='complete'){
                detail.textContent=r.reboot_required===true?tr('操作完成。请重启使所有服务使用新文件。','Complete. Reboot to use all updated files.','İşlem tamamlandı. Yeni dosyaları kullanmak için yeniden başlatın.'):
                    r.reboot_required===false?tr('操作已完成，设备已重启。','Complete. The device has restarted.','İşlem tamamlandı. Cihaz yeniden başlatıldı.'):
                    tr('上次操作已完成。','The last operation completed.','Son işlem tamamlandı.');
            }else if(job.result&&job.result.error){detail.textContent='';detail.appendChild(errorNode(job.result.error));}
            else detail.textContent=tr('尚未应用系统补丁。','No system patches applied yet.','Henüz sistem yaması uygulanmadı.');
            controls();
            progress(r);
        }
        function stopWatch(){if(pollTick)poll.remove(pollTick);watching=false;}
        this.stopWatch=function(){disposed=true;overlay.hidden=true;stopWatch();stopBuildWatch();};
        function interrupted(e){
            // rpcd reload removes and re-registers executable plugins after
            // committed updates. This is not a filesystem transaction failure.
            // Keep this scoped to this object's transport, not arbitrary engine
            // errors, missing methods or denied ACLs.
            if(e&&e.otaBackend)return false;
            return messages.isConnectionError(e)||/RPC call to unisoc-ota\/(?:status|start|rollback|discard_recovery)\b[^\n]*Object not found/i.test(String(e&&e.message||e||''));
        }
        function receive(r){
            if(disposed||rebooting)return;
            error(r);
            initialRetries=0;
            if(pendingError){
                var job=r.job||{}, transaction=r.transaction||{};
                var id=job.patch_id||(job.result||{}).id||transaction.id;
                var accepted=pendingAction&&job.command===pendingAction.command&&id===pendingAction.id&&
                    (job.status==='running'||job.status==='complete'||job.status==='failed');
                var e=pendingError;pendingError=null;
                if(!accepted)failed(e);
            }
            show(r);
        }
        function loadFailed(e){
            initialRetries=0;localPhase=null;busy=false;overlay.hidden=true;stopWatch();
            badge.setAttribute('data-state','pending');
            badge.textContent=tr('状态暂不可用','Status unavailable','Durum kullanılamıyor');
            detail.textContent=tr('无法读取系统补丁状态，请重新打开此页面重试。','Cannot read system patch status. Reopen this page to retry.','Sistem yaması durumu okunamıyor. Yeniden denemek için sayfayı tekrar açın.');
            failed(e);
        }
        function watch(){
            if(watching||disposed||rebooting)return;watching=true;
            pollTick=function(){
                if(disposed||rebooting)return;
                return callStatus().then(function(r){receive(r);pollError=false;if(!busy)stopWatch();}).catch(function(e){
                    if(disposed||rebooting)return;
                    if(initialRetries){
                        if(interrupted(e)&&--initialRetries){phase('reconnecting');return;}
                        loadFailed(e);return;
                    }
                    if(interrupted(e)&&busy){phase('reconnecting');return;}
                    if(!pollError)failed(e);
                    pollError=true;
                });
            };
            poll.add(pollTick,2);
        }
        function refresh(){return callStatus().then(receive).catch(function(e){if(disposed||rebooting)return;if(interrupted(e)&&busy)phase('reconnecting');else failed(e);watch();});}
        function operationFailed(e){
            if(disposed)return Promise.resolve();
            localPhase=null;
            if(interrupted(e)&&pendingAction){
                pendingError=e;
                phase('reconnecting');
                return callStatus().then(function(r){
                    receive(r);if(busy)watch();
                }).catch(function(statusError){if(disposed||rebooting)return;if(!interrupted(statusError)){pendingError=null;failed(statusError);}watch();});
            }
            phase(null);failed(e);return refresh();
        }
        function modal(title,body,confirmLabel,confirm,danger){
            if(disposed)return;
            badge.setAttribute('data-state','pending');badge.textContent=tr('等待确认','Awaiting confirmation','Onay bekleniyor');detail.textContent=title;
            ui.showModal(title,[E('div',{class:'mud mud-ota-modal'},[body,E('div',{class:'mud-ota-modal-actions'},[
                E('button',{type:'button',class:'mud-btn',click:function(){ui.hideModal();show(last);}},tr('取消','Cancel','İptal')),
                E('button',{type:'button',class:'mud-btn '+(danger?'warn':'mud-ota-primary'),click:function(){ui.hideModal();return confirm();}},confirmLabel)])])]);
        }
        function inspectPatch(generated){
            if(disposed)return Promise.resolve();
            phase('checking');
            return (generated?callInspectGenerated():callInspect()).then(function(r){
                if(disposed)return;
                phase(null);error(r);var m=r.manifest;
                if(!m||typeof r.id!=='string'||!/^[a-f0-9]{64}$/.test(r.id))throw new Error('Incomplete patch inspection response');
                var text=E('div',{},[E('p',{},m.base_version+' → '+m.target_version),
                    E('p',{},m.operations.length+tr(' 个系统文件变更；保留配置、插件及用户数据。',' system file changes. Settings, extra plugins and user data are retained.',' sistem dosyası değişikliği. Ayarlar, eklentiler ve kullanıcı verileri korunur.')),
                    E('p',{class:'mud-note'},tr('仅导入可信来源的补丁。哈希校验不等同于发布者签名。','Import only trusted patches. Checksums do not authenticate the publisher.','Yalnızca güvenilir yamaları içe aktarın. Sağlama toplamı yayıncıyı doğrulamaz.'))]);
                if(m.boot)text.appendChild(E('p',{},tr('包含内核／启动镜像与配套模块更新，完成后重启生效。','Includes kernel/boot and matching modules. Reboot after completion.','Çekirdek ve eşleşen modülleri içerir. Tamamlandıktan sonra yeniden başlatın.')));
                if(!r.applicable){text.appendChild(E('p',{},tr('无法应用。冲突文件或依赖：','Cannot apply. Conflicts: ','Uygulanamaz. Çakışmalar: ')+r.conflicts.concat(r.package_conflicts||[]).join(', ')));ui.showModal(tr('无法应用补丁','Patch unavailable','Yama kullanılamıyor'),[text,E('button',{class:'mud-btn',click:ui.hideModal},tr('关闭','Close','Kapat'))]);return;}
                modal(tr('确认系统补丁','Review system patch','Sistem yamasını incele'),text,tr('应用补丁','Apply patch','Yamayı uygula'),function(){
                    if(busy)return;pendingAction={command:'apply',id:r.id};phase('queued');
                    return (generated?callStartGenerated(r.id):callStart(r.id)).then(error).then(function(job){localPhase=null;show(Object.assign({},last,{job:Object.assign({command:'apply',patch_id:r.id},job)}));watch();}).catch(operationFailed);
                });
            }).catch(function(e){phase(null);detail.textContent=tr('本次补丁未导入。','This patch was not imported.','Bu yama içe aktarılmadı.');failed(e);});
        }
        button.onclick=function(){
            if(busy||buildBusy)return;
            phase('upload');
            return ui.uploadFile('/etc/unisoc-ota/upload.ota',button).then(function(){return inspectPatch(false);}).catch(function(e){phase(null);failed(e);});
        };
        rollback.onclick=function(){
            if(busy)return;phase('recovery');
            detail.textContent=tr('正在检查恢复副本…','Checking recovery copy…','Kurtarma kopyası kontrol ediliyor…');
            return callRollbackCheck().then(error).then(function(check){
                phase(null);
                if(!check.available){
                    if(check.reason==='no_update')throw new Error('No completed update to roll back');
                    if(check.reason==='recovery_deleted')throw new Error('Recovery copy was deleted');
                    ui.showModal(tr('暂不能撤销','Rollback unavailable','Geri alma kullanılamıyor'),[E('p',{},tr('以下文件在更新后有变化，未执行任何恢复写入。','These files changed after the update. No recovery writes were made.','Bu dosyalar güncellemeden sonra değişti. Geri yükleme yapılmadı.')),
                        E('div',{style:'max-height:240px;overflow:auto'},(check.conflicts||[]).slice(0,20).map(function(c){return errorNode('Rollback conflict: '+c.path+'; changed='+c.fields.join(','));})),E('button',{class:'mud-btn',click:ui.hideModal},tr('关闭','Close','Kapat'))]);return;
                }
                modal(tr('撤销上次补丁？','Roll back the last patch?','Son yama geri alınsın mı?'),E('p',{},tr('恢复副本检查通过，将恢复为更新前版本。','Recovery checks passed. Restore the previous version.','Kurtarma kontrolleri başarılı. Önceki sürüm geri yüklenecek.')),tr('撤销','Roll back','Geri al'),function(){if(busy)return;var id=(last.transaction||{}).id;pendingAction={command:'rollback',id:id};phase('queued');return callRollback().then(error).then(function(job){localPhase=null;show(Object.assign({},last,{job:Object.assign({command:'rollback',patch_id:id,phase:'rollback'},job)}));watch();}).catch(operationFailed);});
            }).catch(operationFailed);
        };
        discard.onclick=function(){
            if(busy||discard.disabled)return;
            modal(tr('删除恢复副本？','Delete the recovery copy?','Kurtarma kopyası silinsin mi?'),E('div',{},[
                E('p',{},tr('将释放约 ','Reclaim approximately ','Yaklaşık ')+bytes((last.recovery||{}).bytes)+tr(' 存储空间。',' of storage.',' depolama alanı açılır.')),
                E('p',{},tr('删除后无法撤销上次补丁。当前系统、已安装插件、配置及用户数据不会被删除。','The last patch can no longer be undone. Your current system, installed plugins, settings and user data will be kept.','Son yama artık geri alınamaz. Mevcut sistem, eklentiler, ayarlar ve kullanıcı verileri korunur.'))]),tr('确认删除','Delete recovery copy','Kurtarma kopyasını sil'),function(){
                    if(busy)return;phase('discard');
                    return callDiscard().then(error).then(function(){localPhase=null;return refresh();}).catch(operationFailed);
                },true);
        };
        reboot.onclick=function(){
            if(busy||rebooting)return;rebooting=true;phase('reboot');stopWatch();
            detail.textContent=tr('正在请求重启，请等待设备恢复连接…','Requesting reboot. Waiting for the device to reconnect…','Yeniden başlatılıyor. Cihazın bağlanması bekleniyor…');
            return callReboot().then(error).then(function(){ui.awaitReconnect();}).catch(function(e){
                if(messages.isConnectionError(e))ui.awaitReconnect();else{rebooting=false;busy=false;return operationFailed(e);}
            });
        };
        var buildMode=E('select',{id:'ota-build-mode'},[
            E('option',{value:'current'},tr('当前版本 → 新版','Current version → new release','Mevcut sürüm → yeni sürüm')),
            E('option',{value:'pair'},tr('旧版 → 新版（上传两个完整包）','Old → new (upload two full releases)','Eski → yeni (iki tam sürüm yükle)'))]);
        buildMode.value='current';
        var oldUpload=action(tr('上传旧版完整 ZIP','Upload old full ZIP','Eski tam ZIP yükle'),'upload');
        var newUpload=action(tr('上传新版完整 ZIP','Upload new full ZIP','Yeni tam ZIP yükle'),'upload');
        var baselineSave=action(tr('保存当前版本基准','Save current baseline','Mevcut temel sürümü kaydet'),'history');
        var generate=action(tr('生成 .ota','Generate .ota','.ota oluştur'),'history','mud-ota-primary');
        var resultButton=action(tr('查看生成结果','View generated patch','Oluşturulan yamayı göster'),'history');
        var buildDetail=E('div',{class:'mud-ota-build-status',role:'status','aria-live':'polite'});
        var oldSize=E('code'),newSize=E('code'),baselineNote=E('p',{class:'mud-note mud-ota-build-baseline'});
        var oldRow=E('div',{class:'mud-ota-build-upload'},[oldUpload,oldSize,baselineSave]);
        function stopBuildWatch(){if(buildTick)poll.remove(buildTick);buildTick=null;}
        function buildControls(){
            if(!buildMode)return;
            var ready=(buildState.baseline||{}).available===true,uploads=buildState.uploads||{};
            var locked=busy||buildBusy||rebooting||!L.hasViewPermission()||(last.recovery||{}).pending||!!buildState.load_error;
            var pair=buildMode.value==='pair';
            oldRow.hidden=!pair&&ready;baselineSave.hidden=pair||ready;
            buildMode.disabled=locked;oldUpload.disabled=locked;newUpload.disabled=locked;
            baselineSave.disabled=locked||!uploads['old.zip'];
            generate.disabled=locked||!uploads['new.zip']||!(pair?uploads['old.zip']:ready);
            resultButton.hidden=!buildState.download;resultButton.disabled=locked;
            oldSize.textContent=uploads['old.zip']?bytes(uploads['old.zip']):tr('未上传','Not uploaded','Yüklenmedi');
            newSize.textContent=uploads['new.zip']?bytes(uploads['new.zip']):tr('未上传','Not uploaded','Yüklenmedi');
            baselineNote.textContent=ready?tr('原始版本基准：','Clean release baseline: ','Temiz sürüm temeli: ')+(buildState.baseline.version||''):
                tr('首次使用请上传与当前系统对应的完整 ZIP，保存原始版本基准；不会读取你改过的系统文件。','First import the full ZIP matching your current system to save a clean baseline. Modified live files are never used as the baseline.','İlk kullanımda mevcut sistemin tam ZIP dosyasını içe aktarın. Değiştirilmiş canlı dosyalar temel olarak kullanılmaz.');
        }
        buildMode.onchange=buildControls;
        function downloadGenerated(){
            if(!buildState.download)return;
            var job=buildState.job||{},name=('OpenWrt-patch-'+job.base_version+'-to-'+job.target_version).replace(/[^a-zA-Z0-9._+-]/g,'_').slice(0,220)+'.ota';
            var form=E('form',{method:'post',action:L.env.cgi_base+'/cgi-download',enctype:'application/x-www-form-urlencoded',style:'display:none'},[
                E('input',{type:'hidden',name:'sessionid',value:rpc.getSessionID()}),
                E('input',{type:'hidden',name:'path',value:'/etc/unisoc-ota/builder/update.ota'}),
                E('input',{type:'hidden',name:'filename',value:name})]);
            document.body.appendChild(form);form.submit();form.remove();
        }
        function generatedModal(){
            var job=buildState.job||{};
            if(disposed||!buildState.download)return;
            shownBuild=job.id;
            ui.showModal(tr('差分包已生成，是否刷入？','Patch ready. Apply it now?','Yama hazır. Şimdi uygulansın mı?'),[
                E('div',{class:'mud mud-ota-modal'},[
                    E('p',{},job.base_version+' → '+job.target_version),
                    E('p',{},(job.files||0)+tr(' 个文件变更 · ',' file changes · ',' dosya değişikliği · ')+bytes(job.bytes)),
                    E('p',{class:'mud-note'},tr('生成未改动系统。刷入前将继续检查兼容性并请你确认。','Generation has not changed the system. Applying requires compatibility checks and your confirmation.','Oluşturma sistemi değiştirmedi. Uygulamak için uyumluluk kontrolü ve onayınız gerekir.')),
                    E('div',{class:'mud-ota-modal-actions'},[
                        E('button',{type:'button',class:'mud-btn',click:function(){ui.hideModal();}},tr('稍后处理','Later','Daha sonra')),
                        E('button',{type:'button',class:'mud-btn',click:downloadGenerated},tr('下载 .ota','Download .ota','.ota indir')),
                        E('button',{type:'button',class:'mud-btn mud-ota-primary',disabled:!L.hasViewPermission(),click:function(){
                            if(busy||buildBusy)return;ui.hideModal();return inspectPatch(true);
                        }},tr('检查并刷入','Check and apply','Kontrol et ve uygula'))])])]);
        }
        resultButton.onclick=generatedModal;
        function paintBuild(value,notify){
            if(disposed)return;
            error(value);buildState=value;var job=value.job||{};
            buildBusy=job.status==='running';
            var buildPhase=job.phase==='index'?tr('正在分析固件文件','Analyzing firmware files','Ürün yazılımı dosyaları inceleniyor'):
                tr('正在准备生成','Preparing generation','Oluşturma hazırlanıyor');
            buildDetail.textContent=value.load_error?tr('差分生成功能暂不可用，请更新插件后重试。','Patch generation is unavailable. Update the plugin and retry.','Yama oluşturulamıyor. Eklentiyi güncelleyip yeniden deneyin.'):
                buildBusy?tr('正在生成，请稍候；关闭网页不会中断任务。','Generating. You may close this page; the task will continue.','Oluşturuluyor. Sayfayı kapatsanız da görev sürer.')+'\n'+buildPhase+(job.path?' · '+job.path:''):
                job.status==='failed'?tr('生成失败：','Generation failed: ','Oluşturma başarısız: ')+(job.error||''):
                job.status==='complete'?job.mode==='baseline'?tr('当前版本原始基准已保存。','Clean baseline saved.','Temiz temel sürüm kaydedildi.'):
                tr('差分包已生成，可下载或检查后刷入。','Patch ready to download or check and apply.','Yama indirilebilir veya kontrol edilip uygulanabilir.'):
                tr('选择完整固件 ZIP，生成可直接使用的 .ota。','Select full firmware ZIP files to generate a directly usable .ota.','Tam ZIP dosyalarını seçerek doğrudan kullanılabilir .ota oluşturun.');
            controls();
            if(!buildBusy&&!localPhase&&!rebooting&&value.download&&notify)show(last);
            if(buildBusy)watchBuild();else{stopBuildWatch();if(notify&&value.download&&shownBuild!==job.id)generatedModal();}
        }
        function watchBuild(){
            if(buildTick||disposed)return;
            buildTick=function(){return callBuildStatus().then(function(r){paintBuild(r,true);}).catch(function(e){
                if(disposed)return;
                buildDetail.textContent=tr('暂时无法读取进度，正在重新连接；任务不会因此取消。','Progress unavailable. Reconnecting; the task is not cancelled.','İlerleme okunamıyor. Yeniden bağlanılıyor; görev iptal edilmedi.');
            });};
            poll.add(buildTick,2);
        }
        function uploadFull(name,target){
            if(busy||buildBusy)return Promise.resolve();
            buildBusy=true;controls();
            return ui.uploadFile('/etc/unisoc-ota/builder/'+name,target).then(function(){
                return callBuildStatus();
            }).then(function(r){paintBuild(r,false);}).catch(function(e){buildBusy=false;controls();failed(e);});
        }
        oldUpload.onclick=function(){return uploadFull('old.zip',oldUpload);};
        newUpload.onclick=function(){return uploadFull('new.zip',newUpload);};
        function beginBuild(mode){
            if(busy||buildBusy)return Promise.resolve();
            buildBusy=true;controls();
            return callGenerate(mode).then(error).then(function(job){
                paintBuild(Object.assign({},buildState,{job:job,download:false}),false);watchBuild();
            }).catch(function(e){
                // A lost acknowledgement must query the detached task before
                // unlocking controls or deciding that generation failed.
                return callBuildStatus().then(function(r){paintBuild(r,true);if(!buildBusy&&!(r.job||{}).id)failed(e);}).catch(function(){watchBuild();});
            });
        }
        generate.onclick=function(){return beginBuild(buildMode.value);};
        baselineSave.onclick=function(){return beginBuild('baseline');};
        var builderCard=E('section',{class:'mud-card mud-ota-builder'},[
            E('div',{class:'mud-ota-card-head'},[icon('history'),E('h3',{},tr('生成差分包','Generate differential patch','Fark yaması oluştur'))]),
            E('p',{class:'mud-note'},tr('两种方式都基于完整固件的原始文件摘要，保留现有配置、插件和数据。生成完成后可选择是否刷入。','Both modes compare clean release indexes and preserve your settings, plugins and data. Choose whether to apply after generation.','Her iki yöntem temiz sürümleri karşılaştırır; ayarlar, eklentiler ve veriler korunur. Oluşturduktan sonra uygulamayı seçebilirsiniz.')),
            E('div',{class:'mud-ota-build-fields'},[E('label',{for:'ota-build-mode'},tr('生成方式','Source','Kaynak')),buildMode]),
            baselineNote,oldRow,E('div',{class:'mud-ota-build-fields mud-ota-build-upload'},[newUpload,newSize]),
            E('div',{class:'mud-ota-actions'},[generate,resultButton]),buildDetail]);
        paintBuild(buildState,false);
        if(state.load_error){if(interrupted(state.load_error)){phase('reconnecting');watch();}else{initialRetries=0;failed(state.load_error);}}else show(state);
        return E('div',{class:'mud mud-ota'},[E('style',{},CSS),overlay,
            E('header',{class:'mud-ota-heading'},[E('span',{class:'mud-ota-kicker'},tr('系统维护','SYSTEM MAINTENANCE','SİSTEM BAKIMI')),E('h2',{},tr('系统补丁','System patches','Sistem yamaları')),E('p',{class:'mud-note'},tr('只更新变化的系统文件，保留你的插件、配置和数据。','Update changed system files while keeping your plugins, settings and data.','Eklentileri, ayarları ve verileri koruyarak değişen sistem dosyalarını güncelleyin.'))]),
            E('section',{class:'mud-card mud-ota-hero'},[E('div',{class:'mud-ota-version'},[badge,version]),reboot]),
            E('div',{class:'mud-ota-grid'},[
                E('section',{class:'mud-card'},[E('div',{class:'mud-ota-card-head'},[icon('upload'),E('h3',{},tr('导入差异更新','Import differential update','Fark güncellemesini içe aktar'))]),
                    E('p',{class:'mud-note'},tr('支持直接上传 .ota 或包含 .ota 的完整更新 ZIP，自动识别补丁。支持系统、内核、模块、依赖及更新器自身更新。','Upload .ota directly or a complete update ZIP containing .ota. The patch is detected automatically. System, kernel, modules, dependencies and updater are supported.','.ota dosyasını veya onu içeren tam güncelleme ZIP dosyasını yükleyin. Yama otomatik bulunur. Sistem, çekirdek, modüller ve güncelleyici desteklenir.')),
                    E('div',{class:'mud-ota-actions'},[button]),E('p',{class:'mud-note mud-ota-footnote'},tr('无需手动解压。写入前会检查补丁并保存恢复副本。只有完整固件 ZIP 时，请使用下方的差分生成功能。','No manual extraction needed. Checks and a recovery copy precede writes. For a full firmware ZIP without .ota, use the generator below.','Elle açmak gerekmez. Yazmadan önce kontrol ve kurtarma kopyası oluşturulur. .ota içermeyen tam ZIP için aşağıdaki oluşturucuyu kullanın.'))]),
                E('section',{class:'mud-card'},[E('div',{class:'mud-ota-card-head'},[icon('recovery'),E('h3',{},tr('恢复副本','Recovery copy','Kurtarma kopyası'))]),
                    E('div',{class:'mud-ota-space'},[size,E('span',{class:'mud-ota-size-note'},tr('占用存储空间','Storage used','Kullanılan alan'))]),recoveryNote,E('div',{class:'mud-ota-actions'},[rollback,discard])])]),
            builderCard,
            E('section',{class:'mud-card'},[E('div',{class:'mud-ota-card-head'},[icon('history'),E('h3',{},tr('最近操作','Last operation','Son işlem'))]),detail])]);
    },
    unload:function(){if(this.stopWatch)this.stopWatch();},
    handleSaveApply:null,handleSave:null,handleReset:null
});
