package cn.kanokano.openwrt.mobile

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat

/** USB operations do not need LAN permission. Check again at every TCP connection. */
internal object LocalNetworkAccess {
    const val PERMISSION = "android.permission.ACCESS_LOCAL_NETWORK"
    fun required(sdk: Int, target: Int): Boolean = sdk >= 37 && target >= 37
    fun granted(context: Context): Boolean = !required(Build.VERSION.SDK_INT, context.applicationInfo.targetSdkVersion) ||
        ContextCompat.checkSelfPermission(context, PERMISSION) == PackageManager.PERMISSION_GRANTED
    fun require(context: Context) {
        if (!granted(context)) throw SecurityException(
            "Local network permission denied. Allow Nearby devices in app settings / 局域网权限未授权，请在应用设置中允许附近的设备权限")
    }
}
