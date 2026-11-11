package cn.kanokano.openwrt.mobile

import org.junit.Assert.*
import org.junit.Test

class LocalNetworkAccessTest {
    @Test fun onlyAndroid17WithTarget37RequiresPermission() {
        assertFalse(LocalNetworkAccess.required(26, 37))
        assertFalse(LocalNetworkAccess.required(36, 37))
        assertFalse(LocalNetworkAccess.required(37, 36))
        assertTrue(LocalNetworkAccess.required(37, 37))
        assertTrue(LocalNetworkAccess.required(38, 38))
    }
}
