package dev.bookq.reader.trace

/**
 * `bookq.*` systrace/Perfetto sections — the performance-measurement
 * foundation (TraceSectionMetric reads these names).
 */
object BookqTrace {
    const val PAGE_TURN = "bookq.pageTurn"
    const val TREE_LOAD = "bookq.treeLoad"
    const val BOOT = "bookq.boot"

    inline fun <T> section(name: String, block: () -> T): T =
        androidx.tracing.trace(name, block)

    fun begin(name: String) = androidx.tracing.Trace.beginSection(name)

    fun end() = androidx.tracing.Trace.endSection()
}
