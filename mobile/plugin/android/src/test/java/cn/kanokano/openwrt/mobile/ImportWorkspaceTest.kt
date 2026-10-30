package cn.kanokano.openwrt.mobile

import java.io.File
import java.nio.file.Files
import org.junit.Assert.*
import org.junit.Test

class ImportWorkspaceTest {
    @Test fun failedAndSuccessfulPicksCleanOnlyTheirOwnUpload() {
        val root = Files.createTempDirectory("openwrt-import-test").toFile()
        try {
            val saved = File(root, "cached-input").apply { mkdir(); File(this, "release.zip").writeText("saved") }
            repeat(3) {
                val job = ImportWorkspace(root)
                job.file("partial.zip").writeText("partial upload")
                assertTrue(job.cleanup()); assertTrue(job.cleanup())
                assertEquals("saved", File(saved, "release.zip").readText())
            }
            val published = File(root, "published")
            val job = ImportWorkspace(root)
            job.file("complete.zip").writeText("complete")
            assertTrue(job.directory.renameTo(published))
            assertTrue(job.cleanup())
            assertEquals("complete", File(published, "complete.zip").readText())
        } finally { root.deleteRecursively() }
    }
    @Test fun filenamesCannotEscapeThePickerUpload() {
        val root = Files.createTempDirectory("openwrt-import-test").toFile()
        try {
            val job = ImportWorkspace(root)
            try { job.file("../outside"); fail("Expected rejection") } catch (_: IllegalArgumentException) { }
            assertFalse(File(root, "outside").exists()); assertTrue(job.cleanup())
        } finally { root.deleteRecursively() }
    }
}
