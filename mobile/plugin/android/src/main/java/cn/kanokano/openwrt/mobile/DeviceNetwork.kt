package cn.kanokano.openwrt.mobile

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.ParcelFileDescriptor
import java.net.InetAddress

/** A no-internet device hotspot must not send device TCP through cellular. */
class DeviceNetwork(context: Context) {
    private val manager = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager

    fun bindSocket(fd: Int, address: String): Boolean {
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
}
