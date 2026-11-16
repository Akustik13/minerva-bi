package com.minervabi.cards

import android.app.Application

/** Додаток «Minerva Візитки»: фото візиток / флаєрів → черга на телефоні → Minerva (suppliers/app_api.py). */
class App : Application() {
    override fun onCreate() {
        super.onCreate()
        Prefs.init(this)
        CaptureStore.init(this)
        Sync.schedulePeriodic(this)
        Sync.uploadNow(this)
    }
}
