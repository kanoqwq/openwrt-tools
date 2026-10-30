import {beforeEach, afterEach, describe, expect, it, vi} from 'vitest';

const bridge=vi.hoisted(()=>({invoke:vi.fn(),listen:vi.fn(),addPluginListener:vi.fn()}));
vi.mock('@tauri-apps/api/core',()=>({invoke:bridge.invoke,addPluginListener:bridge.addPluginListener}));
vi.mock('@tauri-apps/api/event',()=>({listen:bridge.listen}));

beforeEach(()=>{
  vi.resetModules();vi.clearAllMocks();
  vi.stubGlobal('navigator',{userAgent:'Windows'});
  vi.stubGlobal('document',{documentElement:{classList:{add:vi.fn()}}});
  vi.stubGlobal('localStorage',{getItem:()=> 'en'});
});
afterEach(()=>vi.unstubAllGlobals());

describe.each([false,true])('host protocol: Android=%s',mobile=>{
  beforeEach(()=>vi.stubGlobal('navigator',{userAgent:mobile?'Android':'Windows'}));
  it('selects the correct backend and retains parameters',async()=>{
    const {request}=await import('../src/protocol');
    bridge.invoke.mockResolvedValue(mobile?{result:{accepted:true}}:{accepted:true});
    expect(await request('plan',{serial:'fixture',choices:{method:'tf'}})).toEqual({accepted:true});
    expect(bridge.invoke).toHaveBeenCalledWith(mobile?'plugin:mobile-host|request':'backend_request',mobile?
      {command:'plan',parameters:JSON.stringify({serial:'fixture',choices:{method:'tf'}}),language:'en'}:
      {command:'plan',params:{serial:'fixture',choices:{method:'tf'}}});
  });
  it('uses native file selection without converting cancellation into a path',async()=>{
    const {native}=await import('../src/protocol');
    bridge.invoke.mockResolvedValue(mobile?{value:null}:null);
    expect(await native('pick_package')).toBeNull();
  });
  it('unregisters event listeners',async()=>{
    const {subscribe}=await import('../src/protocol');const stop=vi.fn();
    bridge.addPluginListener.mockResolvedValue({unregister:stop});bridge.listen.mockResolvedValue(stop);
    const handler=vi.fn();const dispose=await subscribe(handler);dispose();expect(stop).toHaveBeenCalledOnce();
  });
});

it.each(['device disconnected',{message:'USB permission denied'},{error:'not enough storage'}])('formats native errors without [object Object]: %j',async value=>{
  const {bridgeError}=await import('../src/protocol');const error=bridgeError(value);
  expect(error).toBeInstanceOf(Error);expect(error.message).not.toContain('[object Object]');
  expect(error.message).not.toBe('');
});

it('propagates Android file picker errors as Error objects',async()=>{
  vi.stubGlobal('navigator',{userAgent:'Android'});const {native}=await import('../src/protocol');
  bridge.invoke.mockRejectedValue({message:'provider unavailable'});
  await expect(native('pick_package')).rejects.toThrow('provider unavailable');
});
