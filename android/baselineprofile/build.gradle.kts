plugins {
    // com.android.test ships inside AGP — no version (catalog alias would
    // conflict with the classpath-bundled plugin).
    id("com.android.test")
    alias(libs.plugins.androidx.baselineprofile)
}

// Self-instrumented test module that generates the Baseline/Startup
// Profiles and hosts the Macrobenchmark suite (issue #302 gates).
// Tests live under src/main — a com.android.test module IS the test APK.
android {
    namespace = "dev.bookq.reader.baselineprofile"
    compileSdk { version = release(37) { minorApiLevel = 2 } }

    defaultConfig {
        minSdk { version = release(35) }
        targetSdk { version = release(37) }
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Tests run against the :app `benchmark` build type (release-shaped,
    // debug-signed — matchingFallbacks resolves it for this module).
    buildTypes {
        create("benchmark") {
            isDebuggable = true
            signingConfig = getByName("debug").signingConfig
            matchingFallbacks += listOf("release")
        }
    }

    targetProjectPath = ":app"
}

dependencies {
    implementation(libs.androidx.benchmark.macro.junit4)
    implementation(libs.androidx.uiautomator)
    implementation(libs.androidx.test.ext.junit)
    implementation(libs.junit)
}

// CP08-J1 is Android 15 (API 35): non-rooted profile collection is
// supported on API 33+, so the physical device works (no GMD needed).
baselineProfile {
    useConnectedDevices = true
}
