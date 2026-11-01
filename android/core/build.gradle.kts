plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(21))
    }
}

dependencies {
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.core)
    testImplementation(libs.junit)
    testImplementation(libs.snakeyaml)
    testImplementation(libs.kotlinx.coroutines.test)
}

tasks.test {
    // The OpenAPI spec lives at <repo>/api — sibling of this android/ dir.
    systemProperty("bookq.apiDir",
        rootProject.projectDir.parentFile.resolve("api").absolutePath)
}
