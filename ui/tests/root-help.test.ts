import {describe, expect, it} from 'vitest';
import {rootHelp, strings, type Language} from '../src/i18n';

const languages = Object.keys(strings) as Language[];

// These devices have no screen: Magisk is reached by turning ADB debugging on and projecting the virtual screen
// (scrcpy), so asking the user to keep the device screen on and unlocked would be an instruction nobody can follow.
function expectScreenlessSteps(text: string) {
  expect(text).toContain('ADB');
  expect(text).toContain('scrcpy');
  expect(text).not.toMatch(/unlocked|亮屏|解锁/);
}

describe('root instructions while waiting for Android', () => {
  it.each(languages)('%s: a device without su is told to install and patch Magisk, and to allow su for adb', language => {
    const text = rootHelp(strings[language], 'magisk');
    expect(text).toBeTruthy();
    expect(text).toContain('Magisk');
    expect(text).toContain('Select and Patch a File');
    expect(text).toContain('Shell');
    expect(text).toContain('Apps and ADB');
    expectScreenlessSteps(text!);
  });

  it.each(languages)('%s: a su that adb is not allowed to use is told how to grant it', language => {
    const text = rootHelp(strings[language], 'root_permission');
    expect(text).toBeTruthy();
    expect(text).toContain('Magisk');
    expect(text).toContain('Grant');
    expect(text).toContain('Shell');
    expect(text).toContain('Apps and ADB');
    expectScreenlessSteps(text!);
  });

  it.each(languages)('%s: the two situations give different instructions', language => {
    expect(rootHelp(strings[language], 'magisk')).not.toBe(rootHelp(strings[language], 'root_permission'));
  });

  it.each(languages)('%s: every other stage keeps the shared text', language => {
    for (const stage of [undefined, 'ready', 'android_boot', 'android_setup', 'usb_debugging', 'debug_authorization', 'root_ready', 'openwrt_boot'])
      expect(rootHelp(strings[language], stage)).toBeUndefined();
  });
});
