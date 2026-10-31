import {HardDrive, Search} from 'lucide-react';
import {Button} from './ui/button';
import {strings, type Language} from '../i18n';
import type {SpaceInfo} from '../protocol';

const size = (n:number) => n >= 1073741824 ? `${(n/1073741824).toFixed(2)} GiB` : `${(n/1048576).toFixed(2)} MiB`;

export function SpaceOverview({info,language,busy,onInspect}:{info?:SpaceInfo;language:Language;busy:boolean;onInspect:()=>void}) {
  const t=strings[language];
  return <section className="space-overview"><h2><HardDrive size={18}/>{t.spaceTitle}</h2><p className="device-hint">{t.spaceHelp}</p>
    <div className="space-actions"><Button variant="outline" disabled={busy} onClick={onInspect}><Search size={16}/>{t.spaceScan}</Button></div>
    {info ? <><dl className="plan-list"><div><dt>{t.spaceDisk}</dt><dd>{size(info.disk_bytes)}</dd></div><div><dt>{t.spaceAndroid}</dt><dd>{size(info.userdata_bytes)}</dd></div><div><dt>{t.spaceTotal}</dt><dd>{size(info.unallocated_bytes)}</dd></div><div><dt>{t.spaceReclaimable}</dt><dd>{size(info.recoverable_bytes)}</dd></div></dl>
      <div className="space-gaps">{info.gaps.map((gap,i)=><div className="space-gap" key={gap.start_bytes}><strong>{t.spaceRange} {i+1} · {size(gap.bytes)}</strong><p>{t.spacePosition}: {size(gap.start_bytes)} → {size(gap.start_bytes+gap.bytes)}</p><small className={gap.recoverable_bytes ? 'good-text' : 'device-hint'}>{gap.recoverable_bytes ? `${t.spaceCanReclaim}: ${size(gap.recoverable_bytes)}` : gap.reason==='filesystem' ? t.spaceOccupied : gap.reason==='alignment' ? t.spaceAlignment : t.spaceNotAdjacent}</small></div>)}</div>
      {!info.gaps.length&&<p className="device-hint">{t.spaceNone}</p>}{!info.recoverable_bytes&&!!info.gaps.length&&<p className="device-hint">{t.spaceBlocked}</p>}
    </> : <p className="device-hint">{t.spaceInspectFirst}</p>}
  </section>;
}
