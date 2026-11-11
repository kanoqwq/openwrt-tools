package cn.kanokano.openwrt.mobile

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.ParcelFileDescriptor
import android.os.Build
import android.content.pm.PackageManager
import org.json.JSONObject
import java.net.InetAddress

/** A no-internet device hotspot must not send device TCP through cellular. */
class DeviceNetwork(private val context: Context) {
    private val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    fun bindSocket(fd: Int, address: String): Boolean {
        LocalNetworkAccess.require(context)
        val target = InetAddress.getByName(address)
        val candidates = manager.allNetworks.mapNotNull { network ->
            val capabilities = manager.getNetworkCapabilities(network) ?: return@mapNotNull null
            if (!capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) return@mapNotNull null
            if (capabilities.hasTransport(NetworkCapabilities.TRANSPORT_VPN)) return@mapNotNull null
            val routes = manager.getLinkProperties(network)?.routes ?: return@mapNotNull null
            val prefix = routes.filter { !it.isDefaultRoute && it.destination.contains(target) }
                .maxOfOrNull { it.destination.prefixLength } ?: return@mapNotNull null
            Pair(network, prefix)
        }
        val selected = candidates.maxByOrNull { it.second }?.first ?: return false
        // Duplicate/close only the wrapper descriptor, never Python's socket.
        ParcelFileDescriptor.fromFd(fd).use { selected.bindSocket(it.fileDescriptor) }
        return true
    }
    fun requireAccess() = LocalNetworkAccess.require(context)
    fun environment(): String {
        val info = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS)
        return JSONObject().put("android_sdk", Build.VERSION.SDK_INT)
            .put("target_sdk", context.applicationInfo.targetSdkVersion)
            .put("version_name", info.versionName)
            .put("version_code", if (Build.VERSION.SDK_INT >= 28) info.longVersionCode else info.versionCode.toLong())
            .put("local_network_permission_required", LocalNetworkAccess.required(Build.VERSION.SDK_INT, context.applicationInfo.targetSdkVersion))
            .put("local_network_permission_declared", info.requestedPermissions.orEmpty().contains(LocalNetworkAccess.PERMISSION))
            .put("local_network_permission_granted", LocalNetworkAccess.granted(context)).toString()
    }
}
