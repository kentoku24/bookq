import com.android.build.api.variant.BuildConfigField

plugins {
    alias(libs.plugins.android.application)
    // Kotlin is built into AGP 9 — only the serialization/compose compiler
    // plugins are applied on top.
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.androidx.baselineprofile)
}

// Release signing comes from ~/.gradle/gradle.properties (or -P flags):
// BOOKQ_KEYSTORE_PATH / BOOKQ_KEYSTORE_PASSWORD / BOOKQ_KEY_ALIAS /
// BOOKQ_KEY_PASSWORD. Absent → the release APK stays unsigned (sideload
// debug or sign later); the build must not fail.
val releaseKeystorePath = providers.gradleProperty("BOOKQ_KEYSTORE_PATH").orNull
val releaseKeystorePassword = providers.gradleProperty("BOOKQ_KEYSTORE_PASSWORD").orNull
val releaseKeyAlias = providers.gradleProperty("BOOKQ_KEY_ALIAS").orNull
val releaseKeyPassword = providers.gradleProperty("BOOKQ_KEY_PASSWORD").orNull
val hasReleaseSigning = !releaseKeystorePath.isNullOrBlank()

android {
    namespace = "dev.bookq.reader"
    // Installed platform is android-37.2 — target API 37 at minor level 2.
    compileSdk { version = release(37) { minorApiLevel = 2 } }

    defaultConfig {
        applicationId = "dev.bookq.reader"
        minSdk { version = release(35) }
        targetSdk { version = release(37) }
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                // hasReleaseSigning guarantees the path is non-null.
                storeFile = file(releaseKeystorePath!!)
                storePassword = releaseKeystorePassword
                this.keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            if (hasReleaseSigning) {
                signingConfig = signingConfigs.getByName("release")
            }
            buildConfigField(
                "boolean",
                "ALLOW_SERVER_URL_EXTRA",
                "false",
            )
        }
        debug {
            applicationIdSuffix = ".debug"
            buildConfigField(
                "boolean",
                "ALLOW_SERVER_URL_EXTRA",
                "true",
            )
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    // WebRTC native libs are shipped per-ABI: split so CP08-J1 (arm64)
    // and the CI emulator (x86_64) each get a right-sized APK; the
    // universal APK stays for convenience installs.
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "x86_64")
            isUniversalApk = true
        }
    }

    kotlin {
        compilerOptions {
            jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_21)
        }
    }
}

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
    }
}

// The baselineprofile plugin's `nonMinified*`/`benchmark*` variants
// initWith their base type, so they inherit release's
// ALLOW_SERVER_URL_EXTRA=false and the measured app would sit on the
// onboarding screen forever. Measurement variants need the extra.
androidComponents {
    onVariants(selector().all()) { variant ->
        val bt = variant.buildType ?: ""
        if (bt.startsWith("nonMinified") || bt.startsWith("benchmark")) {
            variant.buildConfigFields?.put(
                "ALLOW_SERVER_URL_EXTRA",
                BuildConfigField(
                    "boolean",
                    "true",
                    "serverURL intent extra for measurement variants",
                ),
            )
        }
    }
}

// The generated profile lands under src/release/generated — point the
// measurement variants' baselineProfiles source at it so they need no
// checked-in copies. (`named()` can't see plugin-generated variants,
// so wire it inside onVariants.)
androidComponents {
    onVariants(selector().all()) { variant ->
        if (variant.buildType?.startsWith("nonMinified") == true ||
            variant.buildType?.startsWith("benchmark") == true) {
            variant.sources.baselineProfiles?.addStaticSourceDirectory(
                "src/release/generated/baselineProfiles")
        }
    }
}

// M10 audit surface: `./gradlew :app:compileReleaseKotlin` emits the
// stability/metrics reports for the Compose-stability review.
composeCompiler {
    reportsDestination = layout.buildDirectory.dir("compose_reports")
    metricsDestination = layout.buildDirectory.dir("compose_metrics")
    stabilityConfigurationFiles.add(
        layout.projectDirectory.file("../compose-stability.conf"))
}

dependencies {
    implementation(project(":core"))
    implementation(project(":data"))

    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.material3)
    implementation(libs.androidx.material.icons.core)
    implementation(libs.androidx.material.icons.extended)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.androidx.browser)
    implementation(libs.androidx.datastore.preferences)
    implementation(libs.androidx.tracing.ktx)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.core.splashscreen)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.okhttp)
    implementation(libs.okhttp.coroutines)
    implementation(libs.androidx.work.runtime)
    implementation(libs.coil.compose)
    implementation(libs.coil.network.okhttp)
    implementation(libs.androidx.lifecycle.process)
    implementation(libs.androidx.profileinstaller)
    baselineProfile(project(":baselineprofile"))
    implementation(libs.zxing.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)
    implementation(libs.mlkit.barcode.scanning)

    debugImplementation(libs.androidx.compose.ui.tooling)
    debugImplementation(libs.androidx.compose.ui.test.manifest)

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(libs.androidx.test.core)

    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.androidx.test.runner)
    androidTestImplementation(libs.androidx.uiautomator)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
}
