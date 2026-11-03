package dev.bookq.reader

import android.app.Application
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner

class BookqApp : Application() {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        PerfTunables.bootT0 = android.os.SystemClock.uptimeMillis()
        graph = AppGraph(this)
        graph.model.start()
        // App-level foreground edge → the scope's since-poll + pending
        // progress flush (JS visibilitychange parity).
        ProcessLifecycleOwner.get().lifecycle.addObserver(
            object : DefaultLifecycleObserver {
                override fun onResume(owner: LifecycleOwner) {
                    graph.serverScope.value?.setForeground(true)
                }
                override fun onPause(owner: LifecycleOwner) {
                    graph.serverScope.value?.setForeground(false)
                }
            },
        )
    }
}
