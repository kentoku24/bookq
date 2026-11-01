package dev.bookq.reader

import android.app.Application

class BookqApp : Application() {
    lateinit var graph: AppGraph
        private set

    override fun onCreate() {
        super.onCreate()
        graph = AppGraph(this)
        graph.model.start()
    }
}
