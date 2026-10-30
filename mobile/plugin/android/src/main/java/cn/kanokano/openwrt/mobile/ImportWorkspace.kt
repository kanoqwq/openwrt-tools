package cn.kanokano.openwrt.mobile

import java.io.File
import java.util.UUID

/** Owns only this picker upload; published/cached inputs are moved elsewhere. */
class ImportWorkspace(private val root: File) {
    val directory = File(root, UUID.randomUUID().toString()).apply { mkdirs() }
    fun file(name: String): File = File(directory, name).also {
        require(it.canonicalFile.parentFile == directory.canonicalFile) { "Invalid import filename" }
    }
    fun cleanup(): Boolean {
        if (!directory.exists()) return true
        require(directory.canonicalFile.parentFile == root.canonicalFile) { "Import workspace escaped its root" }
        return directory.deleteRecursively()
    }
}
